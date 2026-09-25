"""Run, task and worker management for bookforge (design doc §2, §4, §5.3, §7).

MongoDB holds runs, tasks and workers (collections ``book_runs``,
``book_tasks``, ``book_workers``, ``book_worker_logs``); the position DAG of
each book lives in its own SQLite file (``bookdb.BookDb``).

A Task is a lease on a bundle of positions for one engine kind. The work of
each run lives in the book's ``queue`` table: filled once when the run is
approved, extended as positions are created, and a queued row carries the
id of the task that leases it. All state changes of runs and tasks happen
under ``self.lock``.
"""

import os
import threading
import time
from collections import deque

from bson.objectid import ObjectId
from vtjson import ValidationError, validate

from bookforge import bookdb, policy, propagate, schemas, shogi
from bookforge.validate import check_result

MIN_WORKER_VERSION = 1
TASK_TARGET_SECONDS = 600  # one lease ~ 10 minutes (§7.2)
TASK_MAX_POSITIONS = 5000
DEAD_TASK_SECONDS = 360
RETRY_AFTER = 60

# Preference order when a run offers several builds for one OS.
CPU_FEATURE_ORDER = (
    "avx512vnni",
    "avx512",
    "avxvnni",
    "bmi2",
    "avx2",
    "sse42",
    "sse41",
    "ssse3",
    "sse2",
)

RUN_STATES = ("pending_approval", "active", "paused", "finished", "failed")


class BookforgeError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def run_engines(args):
    """{kind: engine spec} of a run."""
    if args["type"] == "dual":
        return dict(args["engines"])
    return {args["engine"]["kind"]: args["engine"]}


def run_search(args, kind):
    if args["type"] == "dual":
        return args["search"][kind]
    return args["search"]


def select_binary(engine, hw):
    """Pick the distributed build for a worker: same OS, best CPU feature the
    worker reports. Returns (platform key, artifact) or (None, None)."""
    binaries = engine.get("binaries", {})
    flags = set(hw.get("flags", []))
    best = None
    for key, art in binaries.items():
        os_name, _arch, feature = key.split("-", 2)
        if os_name != hw.get("os"):
            continue
        if feature in CPU_FEATURE_ORDER:
            if feature not in flags and feature != "sse2":
                continue
            rank = CPU_FEATURE_ORDER.index(feature)
        else:
            rank = len(CPU_FEATURE_ORDER)  # generic build, e.g. "cuda"
        if best is None or rank < best[0]:
            best = (rank, key, art)
    if best is None:
        return None, None
    return best[1], best[2]


class Bookforge:
    def __init__(self, db, books_dir):
        self.db = db
        self.runs = db["book_runs"]
        self.tasks = db["book_tasks"]
        self.workers = db["book_workers"]
        self.worker_logs = db["book_worker_logs"]
        self.books_dir = books_dir
        os.makedirs(books_dir, exist_ok=True)
        self.lock = threading.RLock()
        self._books = {}
        self._books_lock = threading.Lock()
        self.scheduler = None

    # books

    def book(self, book_id):
        with self._books_lock:
            b = self._books.get(book_id)
            if b is None:
                path = bookdb.book_path(self.books_dir, book_id)
                if not os.path.exists(path):
                    raise BookforgeError(f"unknown book {book_id}", 404)
                b = bookdb.BookDb(path)
                self._books[book_id] = b
            return b

    def create_book(self, book_id, meta):
        try:
            validate(schemas.book_meta, meta, "meta")
        except ValidationError as e:
            raise BookforgeError(str(e))
        path = bookdb.book_path(self.books_dir, book_id)
        if os.path.exists(path):
            raise BookforgeError(f"book {book_id} already exists")
        b = bookdb.BookDb(path)
        for k, v in meta.items():
            b.set_meta(k, v)
        with self._books_lock:
            self._books[book_id] = b
        return b

    def list_books(self):
        return sorted(f[:-3] for f in os.listdir(self.books_dir) if f.endswith(".db"))

    def add_roots(self, book_id, lines, source="sfen", label=None):
        """Register root positions. ``source`` is 'sfen' (one SFEN per line)
        or 'moves' (one move sequence per line, from startpos unless it starts
        with ``sfen ...``). Duplicates (by normalized SFEN) are skipped.
        Returns {"added": [...], "duplicates": [...]}."""
        b = self.book(book_id)
        parsed = []
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                if source == "sfen":
                    sfen = shogi.normalize(line)
                elif source == "moves":
                    start, moves = shogi.parse_moves_line(line)
                    sfen = shogi.positions_along(start, moves)[-1]
                else:
                    raise BookforgeError(f"unknown root source {source}")
                if shogi.terminal_state(sfen) is not None:
                    raise shogi.PositionError("root position is terminal")
            except shogi.PositionError as e:
                raise BookforgeError(f"line {i + 1}: {e}")
            parsed.append((sfen, line))
        added, dups = [], []
        with b.transaction():
            for sfen, line in parsed:
                node_id, _ = b.upsert_node(
                    sfen, 0, policy.priority(0, 0, "nnue", policy.DEFAULT_WEIGHTS)
                )
                if b.add_root(node_id, label or line[:80], source):
                    added.append(sfen)
                else:
                    dups.append(sfen)
        return {"added": added, "duplicates": dups}

    # runs

    def create_run(self, args, username="dev", approved=False, approver=None):
        try:
            validate(schemas.run_args, args, "args")
        except ValidationError as e:
            raise BookforgeError(str(e))
        self.book(args["book_id"])  # must exist
        args = dict(args)
        args.setdefault("priority", 0)
        args.setdefault("throughput", 100)
        args.setdefault("verify_ratio", 0.0)
        now = time.time()
        run = {
            "args": args,
            "username": username,
            "start_time": now,
            "last_updated": now,
            "state": "pending_approval",
            "approver": None,
            "finish_reason": None,
            "failures": 0,
            "results": {
                "positions": 0,
                "rejected": 0,
                "engine_seconds": 0.0,
                "nodes_created": 0,
                "by_kind": {k: 0 for k in run_engines(args)},
            },
        }
        run_id = str(self.runs.insert_one(run).inserted_id)
        if approved:
            self.approve_run(run_id, approver or username)
        return run_id

    def get_run(self, run_id):
        try:
            oid = ObjectId(run_id)
        except Exception:
            return None
        return self.runs.find_one({"_id": oid})

    def _set_run(self, run_id, fields):
        fields["last_updated"] = time.time()
        self.runs.update_one({"_id": ObjectId(run_id)}, {"$set": fields})

    def approve_run(self, run_id, approver):
        with self.lock:
            run = self.get_run(run_id)
            if run is None:
                raise BookforgeError(f"unknown run {run_id}", 404)
            if run["state"] != "pending_approval":
                raise BookforgeError(f"run {run_id} is {run['state']}")
            self._set_run(run_id, {"state": "active", "approver": approver})
            run = self.get_run(run_id)
            if run["args"]["type"] == "expand":
                self.seed_expand(run)
            b = self.book(run["args"]["book_id"])
            for kind in run_engines(run["args"]):
                b.fill_queue(run_id, kind, **self._todo_filter(run["args"], kind))
            return run

    def pause_run(self, run_id, paused=True):
        with self.lock:
            run = self.get_run(run_id)
            if run is None:
                raise BookforgeError(f"unknown run {run_id}", 404)
            want_from, want_to = (
                ("active", "paused") if paused else ("paused", "active")
            )
            if run["state"] != want_from:
                raise BookforgeError(f"run {run_id} is {run['state']}")
            self._set_run(run_id, {"state": want_to})

    def finish_run(self, run_id, reason):
        with self.lock:
            self._set_run(run_id, {"state": "finished", "finish_reason": reason})
            for task in self.tasks.find({"run_id": run_id, "active": True}):
                self._deactivate_task(task)
            run = self.get_run(run_id)
            self.book(run["args"]["book_id"]).clear_queue(run_id)

    def active_runs(self):
        return list(self.runs.find({"state": "active"}))

    # expansion bookkeeping

    def _growing_runs(self, book_id, kind):
        """Expand runs (active or paused) of a book whose engine is ``kind``:
        they must see every position created in the book."""
        return [
            r
            for r in self.runs.find(
                {
                    "args.book_id": book_id,
                    "args.type": "expand",
                    "state": {"$in": ["active", "paused"]},
                }
            )
            if r["args"]["engine"]["kind"] == kind
        ]

    def _enqueue_new(self, book_id, kind, node_ids):
        if not node_ids:
            return
        b = self.book(book_id)
        for run in self._growing_runs(book_id, kind):
            b.enqueue(
                str(run["_id"]),
                kind,
                node_ids,
                ply_max=run["args"]["policy"]["max_ply_from_root"],
            )

    @staticmethod
    def _done_filter(todo_filter):
        return {
            "eval_hash": todo_filter.get("eval_hash"),
            "min_budget": todo_filter["min_budget"],
        }

    def _todo_filter(self, args, kind):
        """What still needs work for a run and kind (BookDb.fill_queue arguments)."""
        search = run_search(args, kind)
        if args["type"] == "expand":
            return {
                "ply_max": args["policy"]["max_ply_from_root"],
                "min_budget": args["policy"]["min_budget_for_expand"],
            }
        target = args.get("target", {})
        return {
            "ply_min": target.get("ply_min"),
            "ply_max": target.get("ply_max"),
            "eval_hash": run_engines(args)[kind]["eval"]["sha256"],
            "min_budget": search["budget"],
        }

    def seed_expand(self, run):
        """Apply the run's expansion policy to positions that already carry a
        qualifying evaluation (e.g. from an earlier run or an import), so the
        frontier is complete before workers start."""
        args = run["args"]
        kind = args["engine"]["kind"]
        b = self.book(args["book_id"])
        min_budget = args["policy"]["min_budget_for_expand"]
        queue = deque(r["node_id"] for r in b.roots())
        seen = set(queue)
        created = []
        while queue:
            nid = queue.popleft()
            node = b.node(nid)
            ev = b.best_eval(nid, kind, min_budget=min_budget)
            if ev is not None:
                created += policy.expand(b, node, kind, b.cands(ev["id"]), args)
            for child in b.children(nid):
                if child["child_id"] not in seen:
                    seen.add(child["child_id"])
                    queue.append(child["child_id"])
        if created:
            self.runs.update_one(
                {"_id": run["_id"]}, {"$inc": {"results.nodes_created": len(created)}}
            )
            self._enqueue_new(args["book_id"], kind, created)
        return len(created)

    # workers

    def register_worker(self, principal, req):
        try:
            validate(schemas.request_version, req, "request")
        except ValidationError as e:
            raise BookforgeError(str(e))
        worker_id = req["worker_uuid"]
        existing = self.workers.find_one({"_id": worker_id})
        if existing is not None and existing.get("token_id") != principal["token_id"]:
            raise BookforgeError("worker_uuid is bound to another token", 403)
        doc = {
            "token_id": principal["token_id"],
            "name": req.get("name", worker_id[:8]),
            "version": req["version"],
            "capabilities": req["capabilities"],
            "hw": req["hw"],
            "bench": req.get("bench", {}),
            "sri": req.get("sri"),
            "last_seen": time.time(),
        }
        self.workers.update_one({"_id": worker_id}, {"$set": doc}, upsert=True)
        accepted = req["version"] >= MIN_WORKER_VERSION
        return {
            "worker_id": worker_id,
            "min_version": MIN_WORKER_VERSION,
            "accepted": accepted,
            "message": "" if accepted else "worker too old, please update",
        }

    def _worker(self, principal, worker_id):
        w = self.workers.find_one({"_id": worker_id})
        if w is None:
            raise BookforgeError(f"unknown worker {worker_id}", 404)
        if w["token_id"] != principal["token_id"]:
            raise BookforgeError("worker does not belong to this token", 403)
        if w.get("blocked"):
            raise BookforgeError(f"worker {worker_id} is blocked", 403)
        return w

    # tasks

    def _task_size(self, worker, kind, search, slots):
        bench = worker.get("bench", {})
        budget = search["budget"]
        if kind == "nnue":
            nps = bench.get("nnue_nps_per_thread", 0)
            if not nps:
                return slots * 2
            if search["mode"] == "st":
                per_slot = max(1, int(TASK_TARGET_SECONDS * nps / budget))
                n = slots * per_slot
            else:
                n = max(1, int(TASK_TARGET_SECONDS * nps * slots / budget))
        else:
            pps = bench.get("dl_playouts_per_sec", 0)
            if not pps:
                return slots * 2
            n = max(slots, int(TASK_TARGET_SECONDS * pps / budget))
        return min(n, TASK_MAX_POSITIONS)

    def _active_slots(self, run_id, kind):
        return sum(
            t["slots"]
            for t in self.tasks.find(
                {"run_id": run_id, "engine_kind": kind, "active": True}, {"slots": 1}
            )
        )

    def _run_order(self, run, kind):
        active = self._active_slots(str(run["_id"]), kind)
        return (
            -run["args"]["priority"],
            active / run["args"]["throughput"],
            run["start_time"],
        )

    def request_task(self, principal, req):
        try:
            validate(schemas.request_task, req, "request")
        except ValidationError as e:
            raise BookforgeError(str(e))
        with self.lock:
            worker = self._worker(principal, req["worker_id"])
            kind = req["capability"]
            if kind not in worker["capabilities"]:
                raise BookforgeError(f"worker did not declare capability {kind}")
            self.workers.update_one(
                {"_id": worker["_id"]}, {"$set": {"last_seen": time.time()}}
            )
            # One worker holds at most one task: a new request supersedes.
            for old in self.tasks.find({"worker_id": worker["_id"], "active": True}):
                self._deactivate_task(old)

            runs = [r for r in self.active_runs() if kind in run_engines(r["args"])]
            runs.sort(key=lambda r: self._run_order(r, kind))
            for run in runs:
                task = self._make_task(run, worker, kind, req)
                if task is None and not self.check_run_finished(run):
                    # the check settles the book, which can add work
                    task = self._make_task(run, worker, kind, req)
                if task is not None:
                    return {"task": task}
            return {"task": None, "retry_after": RETRY_AFTER}

    def _make_task(self, run, worker, kind, req):
        args = run["args"]
        run_id = str(run["_id"])
        engine = run_engines(args)[kind]
        search = run_search(args, kind)
        if engine.get("local_engine") and "local_engine_sha256" in req:
            binary = None
            engine_hash = req["local_engine_sha256"]
        else:
            _platform, binary = select_binary(engine, worker["hw"])
            if binary is None:
                return None
            engine_hash = binary["sha256"]
        n = self._task_size(worker, kind, search, req["slots"])
        b = self.book(args["book_id"])
        oid = ObjectId()
        task_id = str(oid)
        rows = b.lease(
            run_id,
            kind,
            task_id,
            n,
            **self._done_filter(self._todo_filter(args, kind)),
        )
        if not rows:
            return None
        positions = [{"pos_id": r["id"], "sfen": r["sfen"]} for r in rows]
        now = time.time()
        doc = {
            "_id": oid,
            "run_id": run_id,
            "book_id": args["book_id"],
            "worker_id": worker["_id"],
            "engine_kind": kind,
            "engine_hash": engine_hash,
            "eval_hash": engine["eval"]["sha256"],
            "search": search,
            "positions": positions,
            "done": [],
            "active": True,
            "start": now,
            "last_updated": now,
            "slots": req["slots"],
        }
        self.tasks.insert_one(doc)
        engine_out = {
            "kind": kind,
            "binary": binary,
            "eval": engine["eval"],
            "usi_options": engine.get("usi_options", {}),
        }
        return {
            "task_id": task_id,
            "run_id": run_id,
            "lease_expires": now + DEAD_TASK_SECONDS,
            "engine": engine_out,
            "search": search,
            "positions": positions,
        }

    def _task(self, principal, worker_id, task_id):
        self._worker(principal, worker_id)
        task = self.tasks.find_one({"_id": ObjectId(task_id)})
        if task is None:
            raise BookforgeError(f"unknown task {task_id}", 404)
        if task["worker_id"] != worker_id:
            raise BookforgeError("task belongs to another worker", 403)
        return task

    def _deactivate_task(self, task):
        self.book(task["book_id"]).release(str(task["_id"]))
        self.tasks.update_one(
            {"_id": task["_id"]},
            {"$set": {"active": False, "last_updated": time.time()}},
        )

    def beat(self, principal, req):
        try:
            validate(schemas.beat, req, "request")
        except ValidationError as e:
            raise BookforgeError(str(e))
        with self.lock:
            task = self._task(principal, req["worker_id"], req["task_id"])
            run = self.get_run(task["run_id"])
            alive = task["active"] and run is not None and run["state"] == "active"
            if alive:
                self.tasks.update_one(
                    {"_id": task["_id"]}, {"$set": {"last_updated": time.time()}}
                )
            return {"continue": alive}

    def update_task(self, principal, req):
        try:
            validate(schemas.update_task, req, "request")
        except ValidationError as e:
            raise BookforgeError(str(e))
        with self.lock:
            task = self._task(principal, req["worker_id"], req["task_id"])
            run = self.get_run(task["run_id"])
            if run is None:
                raise BookforgeError("run vanished", 404)
            accepted, rejected = self._accept_results(run, task, req["results"])
            if req["final"] and task["active"]:
                task = self.tasks.find_one({"_id": task["_id"]})
                self._deactivate_task(task)
            run = self.get_run(task["run_id"])
            if run["state"] == "active":
                self.check_run_finished(run)
                run = self.get_run(task["run_id"])
            task = self.tasks.find_one({"_id": task["_id"]})
            cont = task["active"] and run["state"] == "active"
            return {"accepted": accepted, "rejected": rejected, "continue": cont}

    def _accept_results(self, run, task, results):
        args = run["args"]
        kind = task["engine_kind"]
        b = self.book(args["book_id"])
        by_id = {p["pos_id"]: p["sfen"] for p in task["positions"]}
        done = set(task.get("done", []))
        accepted, rejected = [], []
        engine_seconds = 0.0
        created = []
        threads = int(run_engines(args)[kind].get("usi_options", {}).get("Threads", 1))
        with b.transaction():
            for res in results:
                pos_id = res["pos_id"]
                if pos_id not in by_id:
                    rejected.append({"pos_id": pos_id, "reason": "not_in_task"})
                    continue
                if pos_id in done:
                    rejected.append({"pos_id": pos_id, "reason": "duplicate"})
                    continue
                reason = check_result(
                    res,
                    sfen=by_id[pos_id],
                    kind=kind,
                    engine_hash=task["engine_hash"],
                    eval_hash=task["eval_hash"],
                    budget=task["search"]["budget"],
                    multipv=task["search"]["multipv"],
                )
                if reason is not None:
                    rejected.append({"pos_id": pos_id, "reason": reason})
                    continue
                b.insert_eval(
                    pos_id,
                    kind=kind,
                    engine_hash=res["engine_hash"],
                    eval_hash=res["eval_hash"],
                    budget=res["budget"],
                    depth=res.get("depth"),
                    seldepth=res.get("seldepth"),
                    elapsed_ms=res.get("elapsed_ms"),
                    worker_id=task["worker_id"],
                    run_id=str(run["_id"]),
                    task_id=str(task["_id"]),
                    cands=res["multipv"],
                )
                if args["type"] == "expand":
                    created += policy.expand(
                        b, b.node(pos_id), kind, res["multipv"], args
                    )
                done.add(pos_id)
                accepted.append(pos_id)
                engine_seconds += res.get("elapsed_ms", 0) / 1000.0 * threads
        if accepted:
            self.tasks.update_one(
                {"_id": task["_id"]},
                {
                    "$push": {"done": {"$each": accepted}},
                    "$set": {"last_updated": time.time()},
                },
            )
            b.complete(str(run["_id"]), kind, accepted)
        self._enqueue_new(args["book_id"], kind, created)
        self.runs.update_one(
            {"_id": run["_id"]},
            {
                "$inc": {
                    "results.positions": len(accepted),
                    "results.rejected": len(rejected),
                    "results.engine_seconds": engine_seconds,
                    "results.nodes_created": len(created),
                    f"results.by_kind.{kind}": len(accepted),
                },
                "$set": {"last_updated": time.time()},
            },
        )
        return accepted, rejected

    def failed_task(self, principal, req):
        try:
            validate(schemas.failed_task, req, "request")
        except ValidationError as e:
            raise BookforgeError(str(e))
        with self.lock:
            task = self._task(principal, req["worker_id"], req["task_id"])
            if task["active"]:
                self._deactivate_task(task)
            self.tasks.update_one(
                {"_id": task["_id"]},
                {"$set": {"failed": True, "message": req.get("message", "")}},
            )
            self.runs.update_one(
                {"_id": ObjectId(task["run_id"])}, {"$inc": {"failures": 1}}
            )
            return {}

    def worker_log(self, principal, req):
        try:
            validate(schemas.worker_log, req, "request")
        except ValidationError as e:
            raise BookforgeError(str(e))
        self._worker(principal, req["worker_id"])
        self.worker_logs.insert_one({**req, "time": time.time()})
        return {}

    # periodic work

    def check_run_finished(self, run):
        """§5.3 stop conditions. Returns True if the run was finished."""
        if run["state"] != "active":
            return False
        args = run["args"]
        stop = args["stop"]
        res = run["results"]
        run_id = str(run["_id"])
        reason = None
        if "max_nodes" in stop and res["positions"] >= stop["max_nodes"]:
            reason = "max_nodes"
        elif (
            "max_core_hours" in stop
            and res["engine_seconds"] >= stop["max_core_hours"] * 3600
        ):
            reason = "max_core_hours"
        elif stop.get("until_frontier_empty"):
            b = self.book(args["book_id"])
            if b.queue_empty(run_id):
                # Settle first: propagation may move a best move onto a
                # position that does not exist yet, which is more work.
                self._settle_book(args["book_id"])
                if b.queue_empty(run_id):
                    reason = "frontier_empty"
        if reason is None:
            return False
        self.finish_run(run_id, reason)
        return True

    def scavenge_dead_tasks(self):
        cutoff = time.time() - DEAD_TASK_SECONDS
        with self.lock:
            dead = list(
                self.tasks.find({"active": True, "last_updated": {"$lt": cutoff}})
            )
            for task in dead:
                print(
                    f"dead task {task['_id']} run {task['run_id']} "
                    f"worker {task['worker_id']}",
                    flush=True,
                )
                self._deactivate_task(task)
            return len(dead)

    def _settle_book(self, book_id):
        """Propagate a book's dirty nodes and expand the best moves that have
        no child position yet. Returns the number of changed values."""
        unexpanded = []
        changed = propagate.propagate_dirty(self.book(book_id), unexpanded=unexpanded)
        if unexpanded:
            self.expand_best_moves(book_id, unexpanded)
        return changed

    def expand_best_moves(self, book_id, unexpanded):
        """§5.1 follow-up: after propagation a node's best move can be one
        that was never expanded (its expanded rival turned out worse), and
        its value then rests on the parent's shallow MultiPV score. Create
        that child for every expand run whose policy reaches it, so the move
        gets searched. Returns the new node ids."""
        b = self.book(book_id)
        created = []
        with self.lock:
            by_kind = {}
            for node_id, kind, move in unexpanded:
                by_kind.setdefault(kind, []).append((node_id, move))
            for kind, items in by_kind.items():
                runs = self._growing_runs(book_id, kind)
                if not runs:
                    continue
                new_ids = []
                for node_id, move in items:
                    node = b.node(node_id)
                    ply = node["ply_min"] or 0
                    run = next(
                        (
                            r
                            for r in runs
                            if ply + 1 <= r["args"]["policy"]["max_ply_from_root"]
                            and b.is_done(
                                node_id,
                                kind,
                                min_budget=r["args"]["policy"]["min_budget_for_expand"],
                            )
                        ),
                        None,
                    )
                    if run is None:
                        continue
                    prio = policy.priority(
                        ply + 1, 0, kind, policy.weights_for(run["args"])
                    )
                    try:
                        child_id, new = policy.create_child(
                            b, node_id, node["sfen"], move, ply + 1, prio
                        )
                    except shogi.PositionError:
                        continue
                    if new:
                        new_ids.append(child_id)
                        self.runs.update_one(
                            {"_id": run["_id"]}, {"$inc": {"results.nodes_created": 1}}
                        )
                self._enqueue_new(book_id, kind, new_ids)
                created += new_ids
        return created

    def propagate_dirty(self):
        changed = 0
        for book_id in self.list_books():
            changed += self._settle_book(book_id)
        return changed

    def check_runs(self):
        with self.lock:
            for run in self.active_runs():
                self.check_run_finished(run)

    def schedule_tasks(self):
        from fishtest.scheduler import Scheduler

        if self.scheduler is None:
            self.scheduler = Scheduler(jitter=0.05)
        self.scheduler.create_task(30.0, self.propagate_dirty)
        self.scheduler.create_task(60.0, self.scavenge_dead_tasks)
        self.scheduler.create_task(60.0, self.check_runs)

    def shutdown(self):
        if self.scheduler is not None:
            self.scheduler.stop()
            self.scheduler.join()
            self.scheduler = None
        with self._books_lock:
            for b in self._books.values():
                b.close()
            self._books.clear()

    # reporting

    def run_status(self, run_id):
        run = self.get_run(run_id)
        if run is None:
            raise BookforgeError(f"unknown run {run_id}", 404)
        b = self.book(run["args"]["book_id"])
        roots = [
            {
                "sfen": r["sfen"],
                "label": r["label"],
                "value_nnue": r["value_nnue"],
                "value_dl": r["value_dl"],
            }
            for r in b.roots()
        ]
        return {
            "run_id": run_id,
            "state": run["state"],
            "finish_reason": run["finish_reason"],
            "results": run["results"],
            "queue": b.queue_counts(run_id),
            "book": b.counts(),
            "roots": roots,
        }


__all__ = ["Bookforge", "BookforgeError"]
