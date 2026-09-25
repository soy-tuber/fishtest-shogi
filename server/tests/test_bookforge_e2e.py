"""Phase 1 acceptance (design doc §12): a dummy worker drives runs through the
HTTP API until they finish. MongoDB is replaced by mongomock so the test runs
anywhere; the SQLite books are real files."""

import os
import tempfile
import time
import unittest
from unittest import mock

import mongomock
from fastapi.testclient import TestClient

from bookforge import policy, propagate, shogi
from bookforge.app import create_app
from bookforge.dummy_worker import FAKE_HASH, DummyWorker

H_BIN = "a" * 64
H_EVAL = "b" * 64
H_EVAL2 = "c" * 64
H_MODEL = "d" * 64


def nnue_engine(eval_hash=H_EVAL):
    return {
        "kind": "nnue",
        "family": "yaneuraou",
        "binaries": {
            "linux-x64-avx2": {"url": "https://example.invalid/yo", "sha256": H_BIN},
            "windows-x64-avx512vnni": {
                "url": "https://example.invalid/yo.exe",
                "sha256": "e" * 64,
            },
        },
        "eval": {"url": "https://example.invalid/nn.bin", "sha256": eval_hash},
        "usi_options": {"Hash": 16, "Threads": 1, "BookFile": "no_book"},
    }


def dl_engine():
    return {
        "kind": "dl",
        "local_engine": True,
        "eval": {"url": "https://example.invalid/model.onnx", "sha256": H_MODEL},
    }


def expand_args(**over):
    args = {
        "type": "expand",
        "book_id": "t1",
        "engine": nnue_engine(),
        "search": {"budget": 1000, "multipv": 3, "mode": "st"},
        "policy": {
            "self_side": "gote",
            "self_eval_diff": 0,
            "opp_eval_diff": 60,
            "max_ply_from_root": 3,
            "min_budget_for_expand": 0,
        },
        "stop": {"until_frontier_empty": True},
    }
    args.update(over)
    return args


class BookforgeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ, {"BOOKFORGE_AUTH": "stub"})
        env.start()
        self.addCleanup(env.stop)
        self.db = mongomock.MongoClient()["bookforge_tests"]
        app = create_app(db=self.db, books_dir=self.tmp.name, schedule=False)
        self.client = TestClient(app)
        self.client.__enter__()
        self.bf = app.state.bookforge
        self.bf.create_book("t1", {"self_side": "gote"})
        self.bf.add_roots("t1", ["startpos moves 7g7f 3c3d"], source="moves")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def post(self, path, body, status=200, token=None):
        headers = {"X-Bookforge-Dev-Token": token} if token else {}
        r = self.client.post(path, json=body, headers=headers)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def worker(self, capability="nnue", seed=1, slots=4):
        w = DummyWorker(self.post, capability=capability, slots=slots, seed=seed)
        w.register()
        return w

    def drain(self, *workers, max_rounds=500):
        """Let the workers take tasks until none of them gets one."""
        for _ in range(max_rounds):
            if all(w.run_one_task() is None for w in workers):
                return
        self.fail("workers never ran out of work")


class TestExpandRun(BookforgeTestCase):
    def test_run_to_completion(self):
        run_id = self.bf.create_run(expand_args(), approved=True)
        self.drain(self.worker())

        run = self.bf.get_run(run_id)
        self.assertEqual(run["state"], "finished")
        self.assertEqual(run["finish_reason"], "frontier_empty")
        self.assertEqual(run["results"]["rejected"], 0)
        self.assertTrue(self.bf.book("t1").queue_empty(run_id))

        book = self.bf.book("t1")
        # Every non-terminal node within the ply limit has an NNUE eval.
        self.assertEqual(book.select_todo("nnue", limit=1, ply_max=3), [])
        counts = book.counts()
        # at least the best line reaches max_ply_from_root
        self.assertGreaterEqual(counts["nodes"], 4)
        self.assertEqual(book.query_one("SELECT MAX(ply_min) m FROM node")["m"], 3)
        self.assertEqual(run["results"]["positions"], counts["evals"]["nnue"])

        # The tree obeys the asymmetric policy: at gote (self) nodes only the
        # best candidate is expanded, at sente nodes everything within 60cp.
        args = run["args"]
        for node in book.query("SELECT * FROM node WHERE status = 2"):
            ev = book.best_eval(node["id"], "nnue")
            cands = book.cands(ev["id"])
            expected = set()
            if node["ply_min"] < 3:
                turn = shogi.side_to_move(node["sfen"])
                expected = {
                    m
                    for m, _d in policy.select_moves(
                        args["policy"], "nnue", turn, cands
                    )
                }
            got = {r["move"] for r in book.children(node["id"])}
            # transpositions can add edges from a shorter path, never remove
            self.assertTrue(expected <= got, (node["sfen"], expected, got))

        # Propagation: a root value appears, and incremental == full recompute.
        self.bf.propagate_dirty()
        root = book.roots()[0]
        self.assertIsNotNone(root["value_nnue"])
        self.assertIsNone(root["value_dl"])
        self.assertEqual(propagate.propagate_all(book), 0)

    def test_max_nodes_stops_run(self):
        run_id = self.bf.create_run(
            expand_args(
                stop={"max_nodes": 5},
                policy={**expand_args()["policy"], "max_ply_from_root": 20},
            ),
            approved=True,
        )
        self.drain(self.worker(slots=1))
        run = self.bf.get_run(run_id)
        self.assertEqual(run["state"], "finished")
        self.assertEqual(run["finish_reason"], "max_nodes")
        self.assertGreaterEqual(run["results"]["positions"], 5)

    def test_second_run_continues_from_existing_evals(self):
        shallow = expand_args(
            policy={**expand_args()["policy"], "max_ply_from_root": 2}
        )
        self.bf.create_run(shallow, approved=True)
        w = self.worker()
        self.drain(w)
        book = self.bf.book("t1")
        self.assertEqual(book.query_one("SELECT MAX(ply_min) m FROM node")["m"], 2)
        evals_before = book.counts()["evals"]["nnue"]

        # Approving a deeper run applies its policy to the evaluated ply-2
        # nodes right away, without searching them again.
        run_id = self.bf.create_run(expand_args(), approved=True)
        run = self.bf.get_run(run_id)
        self.assertGreater(run["results"]["nodes_created"], 0)
        self.assertEqual(book.query_one("SELECT MAX(ply_min) m FROM node")["m"], 3)
        self.drain(w)
        run = self.bf.get_run(run_id)
        self.assertEqual(run["state"], "finished")
        self.assertEqual(
            book.counts()["evals"]["nnue"], evals_before + run["results"]["positions"]
        )
        self.assertEqual(
            book.query_one(
                "SELECT COUNT(*) c FROM eval GROUP BY node_id ORDER BY c DESC"
            )["c"],
            1,
        )

    def test_parallel_expand_runs_share_new_positions(self):
        # Two expand runs on one book: positions created by either run's
        # results enter both queues, so neither finishes with holes.
        deep = expand_args(policy={**expand_args()["policy"], "max_ply_from_root": 4})
        a = self.bf.create_run(expand_args(), approved=True)
        b_id = self.bf.create_run(deep, approved=True)
        self.drain(self.worker())
        book = self.bf.book("t1")
        for run_id in (a, b_id):
            self.assertEqual(self.bf.get_run(run_id)["state"], "finished")
        self.assertEqual(book.select_todo("nnue", limit=1, ply_max=4), [])

    def test_best_move_that_was_never_expanded_gets_expanded(self):
        # Sente is "self": at the root only the best move (7g7f, +50) is
        # expanded. Its child turns out bad (+400 for gote), so the root's
        # best move becomes 2g2f (+30), which was never expanded. The server
        # must create and search it before the run can finish.
        self.bf.create_book("t3", {"self_side": "sente"})
        self.bf.add_roots("t3", [shogi.STARTPOS])
        args = expand_args(
            book_id="t3",
            search={"budget": 1000, "multipv": 2, "mode": "st"},
            policy={
                **expand_args()["policy"],
                "self_side": "sente",
                "opp_eval_diff": 0,
                "max_ply_from_root": 1,
            },
        )
        run_id = self.bf.create_run(args, approved=True)
        w = self.worker()
        book = self.bf.book("t3")

        def search(scores):
            task = self.post(
                "/api/request_task",
                {"worker_id": w.worker_id, "capability": "nnue", "slots": 1},
            )["task"]
            self.assertIsNotNone(task)
            results = []
            for pos in task["positions"]:
                moves = scores[pos["sfen"]]
                results.append(
                    {
                        "pos_id": pos["pos_id"],
                        "engine_hash": H_BIN,
                        "eval_hash": H_EVAL,
                        "budget": 1000,
                        "multipv": [{"move": m, "score_cp": v} for m, v in moves],
                    }
                )
            res = self.post(
                "/api/update_task",
                {
                    "worker_id": w.worker_id,
                    "task_id": task["task_id"],
                    "final": True,
                    "results": results,
                },
            )
            self.assertEqual(res["rejected"], [])
            return [p["sfen"] for p in task["positions"]]

        after_76 = shogi.apply_move(shogi.STARTPOS, "7g7f")
        after_26 = shogi.apply_move(shogi.STARTPOS, "2g2f")
        scores = {
            shogi.STARTPOS: [("7g7f", 50), ("2g2f", 30)],
            after_76: [("3c3d", 400), ("8c8d", 380)],
            after_26: [("8c8d", -10), ("3c3d", -20)],
        }
        self.assertEqual(search(scores), [shogi.STARTPOS])
        root = book.roots()[0]["node_id"]
        self.assertEqual([r["move"] for r in book.children(root)], ["7g7f"])
        self.assertEqual(search(scores), [after_76])
        # the frontier looked empty, but settling found 2g2f
        self.assertEqual(self.bf.get_run(run_id)["state"], "active")
        self.assertEqual(
            sorted(r["move"] for r in book.children(root)), ["2g2f", "7g7f"]
        )
        self.assertEqual(search(scores), [after_26])
        self.assertEqual(self.bf.get_run(run_id)["state"], "finished")
        self.bf.propagate_dirty()
        # root = max(-400 via 7g7f, +10 via 2g2f)
        self.assertEqual(book.node(root)["value_nnue"], 10)

    def test_pending_run_gets_no_work(self):
        run_id = self.bf.create_run(expand_args())
        w = self.worker()
        self.assertIsNone(w.run_one_task())
        self.bf.approve_run(run_id, "approver")
        self.assertIsNotNone(w.run_one_task())


class TestWorkerApi(BookforgeTestCase):
    def setUp(self):
        super().setUp()
        self.run_id = self.bf.create_run(expand_args(), approved=True)
        self.w = self.worker()

    def request(self):
        return self.post(
            "/api/request_task",
            {"worker_id": self.w.worker_id, "capability": "nnue", "slots": 2},
        )["task"]

    def test_task_payload(self):
        task = self.request()
        self.assertEqual(task["run_id"], self.run_id)
        # linux worker with avx2 gets the linux build
        self.assertEqual(task["engine"]["binary"]["sha256"], H_BIN)
        self.assertEqual(task["engine"]["eval"]["sha256"], H_EVAL)
        self.assertEqual(task["search"], {"budget": 1000, "multipv": 3, "mode": "st"})
        self.assertEqual(len(task["positions"]), 1)  # only the root exists yet
        self.assertNotIn(" 1", task["positions"][0]["sfen"][-2:])

    def test_rejected_results(self):
        task = self.request()
        pos = task["positions"][0]
        base = {
            "pos_id": pos["pos_id"],
            "engine_hash": H_BIN,
            "eval_hash": H_EVAL,
            "budget": 1000,
        }
        res = self.post(
            "/api/update_task",
            {
                "worker_id": self.w.worker_id,
                "task_id": task["task_id"],
                "final": False,
                "results": [
                    {**base, "multipv": [{"move": "1a1b", "score_cp": 0}]},
                    {
                        **base,
                        "eval_hash": H_EVAL2,
                        "multipv": [{"move": "2g2f", "score_cp": 0}],
                    },
                    {
                        **base,
                        "pos_id": 999,
                        "multipv": [{"move": "2g2f", "score_cp": 0}],
                    },
                ],
            },
        )
        self.assertEqual(res["accepted"], [])
        self.assertEqual(
            [r["reason"] for r in res["rejected"]],
            ["illegal_move", "eval_hash_mismatch", "not_in_task"],
        )
        self.assertTrue(res["continue"])
        res = self.post(
            "/api/update_task",
            {
                "worker_id": self.w.worker_id,
                "task_id": task["task_id"],
                "final": True,
                "results": [{**base, "multipv": [{"move": "2g2f", "score_cp": 30}]}],
            },
        )
        self.assertEqual(res["accepted"], [pos["pos_id"]])
        # the same result again is a duplicate
        res = self.post(
            "/api/update_task",
            {
                "worker_id": self.w.worker_id,
                "task_id": task["task_id"],
                "final": True,
                "results": [{**base, "multipv": [{"move": "2g2f", "score_cp": 30}]}],
            },
        )
        self.assertEqual(res["rejected"][0]["reason"], "duplicate")

    def test_schema_errors(self):
        self.post("/api/request_task", {"worker_id": self.w.worker_id}, status=400)
        self.post(
            "/api/request_task",
            {"worker_id": self.w.worker_id, "capability": "gpu", "slots": 1},
            status=400,
        )
        r = self.client.post("/api/beat", content=b"not json")
        self.assertEqual(r.status_code, 400)

    def test_token_binding(self):
        task = self.request()
        body = {"worker_id": self.w.worker_id, "task_id": task["task_id"]}
        self.post("/api/beat", body, status=403, token="other-token")
        # a worker uuid registered by one token cannot be taken over
        self.post(
            "/api/request_version",
            {
                "worker_uuid": self.w.worker_id,
                "version": 1,
                "capabilities": ["nnue"],
                "hw": {"os": "linux"},
            },
            status=403,
            token="other-token",
        )
        self.assertEqual(self.post("/api/beat", body), {"continue": True})

    def test_failed_task_releases_positions(self):
        task = self.request()
        book = self.bf.book("t1")
        self.assertEqual(book.queue_counts(self.run_id)["nnue"]["leased"], 1)
        self.post(
            "/api/failed_task",
            {
                "worker_id": self.w.worker_id,
                "task_id": task["task_id"],
                "message": "crash",
            },
        )
        self.assertEqual(book.queue_counts(self.run_id)["nnue"]["leased"], 0)
        self.assertEqual(self.bf.get_run(self.run_id)["failures"], 1)
        self.assertEqual(
            self.post(
                "/api/beat", {"worker_id": self.w.worker_id, "task_id": task["task_id"]}
            ),
            {"continue": False},
        )
        # the root is handed out again
        self.assertEqual(self.request()["positions"], task["positions"])

    def test_scavenge_dead_task(self):
        task = self.request()
        other = self.worker(seed=12345)
        self.assertIsNone(other.run_one_task())  # the only position is leased
        with mock.patch("time.time", return_value=time.time() + 1000):
            self.assertEqual(self.bf.scavenge_dead_tasks(), 1)
        self.assertEqual(other.run_one_task(), 1)
        beat = self.post(
            "/api/beat", {"worker_id": self.w.worker_id, "task_id": task["task_id"]}
        )
        self.assertEqual(beat, {"continue": False})

    def test_leases_survive_restart(self):
        task = self.request()
        self.bf.shutdown()
        from bookforge.service import Bookforge

        again = Bookforge(self.db, self.tmp.name)
        self.addCleanup(again.shutdown)
        counts = again.book("t1").queue_counts(self.run_id)
        self.assertEqual(counts["nnue"]["leased"], len(task["positions"]))
        # the leased root is not handed out twice
        other = DummyWorker(self.post, seed=777)
        other.register()
        self.bf = again
        self.client.app.state.bookforge = again
        self.assertIsNone(other.run_one_task())

    def test_worker_log(self):
        self.post("/api/worker_log", {"worker_id": self.w.worker_id, "message": "hi"})
        self.assertEqual(self.db["book_worker_logs"].count_documents({}), 1)


class TestOtherRuns(BookforgeTestCase):
    def test_reeval_after_expand(self):
        self.bf.create_run(expand_args(), approved=True)
        w = self.worker()
        self.drain(w)
        book = self.bf.book("t1")
        n_eval = book.counts()["evals"]["nnue"]

        run_id = self.bf.create_run(
            {
                "type": "reeval",
                "book_id": "t1",
                "engine": nnue_engine(eval_hash=H_EVAL2),
                "search": {"budget": 5000, "multipv": 3, "mode": "st"},
                "target": {"ply_max": 1},
                "stop": {"until_frontier_empty": True},
            },
            approved=True,
        )
        self.drain(w)
        run = self.bf.get_run(run_id)
        self.assertEqual(run["state"], "finished")
        in_scope = book.query_one(
            "SELECT COUNT(*) c FROM node WHERE ply_min <= 1 AND status != 4"
        )["c"]
        self.assertEqual(run["results"]["positions"], in_scope)
        self.assertEqual(book.counts()["evals"]["nnue"], n_eval + in_scope)
        # reeval does not grow the book
        self.assertEqual(run["results"]["nodes_created"], 0)
        # the deeper evaluation is the one propagation uses
        root = book.roots()[0]["node_id"]
        self.assertEqual(book.best_eval(root, "nnue")["eval_hash"], H_EVAL2)

    def test_dual_run(self):
        self.bf.create_run(expand_args(), approved=True)
        self.drain(self.worker())
        run_id = self.bf.create_run(
            {
                "type": "dual",
                "book_id": "t1",
                "engines": {"nnue": nnue_engine(eval_hash=H_EVAL2), "dl": dl_engine()},
                "search": {
                    "nnue": {"budget": 1000, "multipv": 3, "mode": "st"},
                    "dl": {"budget": 800, "multipv": 3, "mode": "st"},
                },
                "target": {"ply_max": 2},
                "stop": {"until_frontier_empty": True},
            },
            approved=True,
        )
        nnue, dl = self.worker(seed=3), self.worker("dl", seed=4)
        self.drain(nnue, dl)
        run = self.bf.get_run(run_id)
        self.assertEqual(run["state"], "finished")
        self.assertGreater(run["results"]["by_kind"]["dl"], 0)
        self.assertGreater(run["results"]["by_kind"]["nnue"], 0)
        book = self.bf.book("t1")
        # DL results carry the locally declared engine hash
        self.assertEqual(
            book.query_one(
                "SELECT DISTINCT engine_hash h FROM eval WHERE engine_kind='dl'"
            )["h"],
            FAKE_HASH,
        )
        self.bf.propagate_dirty()
        root = book.roots()[0]
        self.assertIsNotNone(root["value_nnue"])
        self.assertIsNotNone(root["value_dl"])
        self.assertIsInstance(root["value_nnue"], int)
        self.assertTrue(0.0 <= root["value_dl"] <= 1.0)


class TestRoots(BookforgeTestCase):
    def test_duplicates_and_errors(self):
        from bookforge.service import BookforgeError

        # setUp registered "7g7f 3c3d"; the same position as a numbered SFEN
        # is a duplicate, since identity ignores the move number.
        after = "lnsgkgsnl/1r5b1/pppppp1pp/6p2/9/2P6/PP1PPPPPP/1B5R1/LNSGKGSNL b - 3"
        res = self.bf.add_roots("t1", [after])
        self.assertEqual((len(res["added"]), len(res["duplicates"])), (0, 1))
        res = self.bf.add_roots(
            "t1", ["2g2f", "", "startpos moves 7g7f 3c3d"], source="moves"
        )
        self.assertEqual((len(res["added"]), len(res["duplicates"])), (1, 1))
        self.assertEqual(len(self.bf.book("t1").roots()), 2)
        for lines, source in [
            (["7g7f 7g7f"], "moves"),
            (["garbage"], "sfen"),
            (["9/9/9/9/9/9/9/9/9 b -"], "sfen"),
        ]:
            with self.assertRaises(BookforgeError):
                self.bf.add_roots("t1", lines, source=source)


class TestCli(BookforgeTestCase):
    def test_commands(self):
        import contextlib
        import io
        import json

        from bookforge import cli

        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(list(argv), service=self.bf)
            return code, (json.loads(out.getvalue()) if code == 0 else None)

        self.assertEqual(run("create-book", "t2", "--self-side", "sente")[0], 0)
        code, out = run("add-roots", "t2", "--moves", "2g2f 8c8d", "--moves", "7g7f")
        self.assertEqual((code, len(out["added"])), (0, 2))
        path = os.path.join(self.tmp.name, "run.json")
        with open(path, "w") as f:
            json.dump(expand_args(book_id="t2"), f)
        code, out = run("create-run", path)
        self.assertEqual(out["state"], "pending_approval")
        run_id = out["run_id"]
        self.assertEqual(run("approve", run_id)[1]["state"], "active")
        self.assertEqual(run("pause", run_id)[1]["state"], "paused")
        self.assertEqual(run("resume", run_id)[1]["state"], "active")
        self.drain(self.worker())
        code, out = run("status", run_id)
        self.assertEqual(out["state"], "finished")
        self.assertEqual(len(out["roots"]), 2)
        self.assertEqual(run("propagate", "t2", "--all")[0], 0)
        self.assertEqual(len(run("list-runs")[1]), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run("create-book", "../evil", "--self-side", "gote")[0], 1)


if __name__ == "__main__":
    unittest.main()
