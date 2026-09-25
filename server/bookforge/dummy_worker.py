"""Dummy worker for Phase 1 (design doc §12): plays the worker protocol but
"searches" by picking random legal moves with random scores.

    uv run python -m bookforge.dummy_worker --url http://127.0.0.1:8000

The protocol code only needs a ``post(path, json) -> dict`` callable, so the
tests drive it through FastAPI's TestClient.
"""

import argparse
import random
import sys
import time
import uuid

import cshogi

VERSION = 1
FAKE_HASH = "0" * 64


def fake_search(sfen, kind, multipv, rng):
    board = cshogi.Board(sfen + " 1")
    moves = [cshogi.move_to_usi(m) for m in board.legal_moves]
    rng.shuffle(moves)
    moves = moves[:multipv]
    out = []
    if kind == "nnue":
        scores = sorted((rng.randint(-300, 300) for _ in moves), reverse=True)
        for move, score in zip(moves, scores):
            out.append({"move": move, "score_cp": score, "pv": move})
    else:
        scores = sorted((rng.random() for _ in moves), reverse=True)
        for move, score in zip(moves, scores):
            out.append({"move": move, "winrate": round(score, 4), "pv": move})
    return out


class DummyWorker:
    def __init__(
        self, post, capability="nnue", slots=4, seed=0, chunk=8, worker_uuid=None
    ):
        self.post = post
        self.capability = capability
        self.slots = slots
        self.rng = random.Random(seed)
        self.chunk = chunk
        self.worker_id = worker_uuid or str(uuid.UUID(int=self.rng.getrandbits(128)))

    def register(self):
        res = self.post(
            "/api/request_version",
            {
                "worker_uuid": self.worker_id,
                "version": VERSION,
                "capabilities": [self.capability],
                "hw": {"os": "linux", "threads": self.slots, "flags": ["avx2"]},
                "bench": {},
                "name": "dummy",
            },
        )
        if not res.get("accepted"):
            raise RuntimeError(f"worker rejected: {res}")
        return res

    def run_one_task(self):
        """Request and process one task. Returns the number of positions
        searched, or None if the server had no work."""
        res = self.post(
            "/api/request_task",
            {
                "worker_id": self.worker_id,
                "capability": self.capability,
                "slots": self.slots,
                "local_engine_sha256": FAKE_HASH,
            },
        )
        task = res.get("task")
        if task is None:
            return None
        engine = task["engine"]
        engine_hash = engine["binary"]["sha256"] if engine["binary"] else FAKE_HASH
        eval_hash = engine["eval"]["sha256"]
        search = task["search"]
        batch = []
        n = 0
        for i, pos in enumerate(task["positions"]):
            batch.append(
                {
                    "pos_id": pos["pos_id"],
                    "engine_hash": engine_hash,
                    "eval_hash": eval_hash,
                    "budget": search["budget"],
                    "depth": 1,
                    "elapsed_ms": 10,
                    "multipv": fake_search(
                        pos["sfen"], engine["kind"], search["multipv"], self.rng
                    ),
                }
            )
            final = i == len(task["positions"]) - 1
            if len(batch) >= self.chunk or final:
                r = self.post(
                    "/api/update_task",
                    {
                        "worker_id": self.worker_id,
                        "task_id": task["task_id"],
                        "final": final,
                        "results": batch,
                    },
                )
                if r.get("rejected"):
                    raise RuntimeError(f"results rejected: {r['rejected']}")
                n += len(batch)
                batch = []
                if not r["continue"]:
                    break
        return n


def _http_post(base_url, token):
    import requests

    session = requests.Session()
    if token:
        session.headers["X-Bookforge-Dev-Token"] = token

    def post(path, body):
        r = session.post(base_url.rstrip("/") + path, json=body, timeout=60)
        r.raise_for_status()
        return r.json()

    return post


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--capability", choices=["nnue", "dl"], default="nnue")
    p.add_argument("--slots", type=int, default=4)
    p.add_argument("--token", default=None)
    p.add_argument("--once", action="store_true", help="stop when no work is left")
    a = p.parse_args(argv)
    w = DummyWorker(
        _http_post(a.url, a.token),
        a.capability,
        a.slots,
        seed=random.randrange(1 << 30),
    )
    w.register()
    while True:
        n = w.run_one_task()
        if n is None:
            if a.once:
                return 0
            time.sleep(10)
        else:
            print(f"searched {n} positions", flush=True)


if __name__ == "__main__":
    sys.exit(main())
