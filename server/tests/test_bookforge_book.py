"""Positions, expansion policy and result validation (design doc §3, §5.1, §7.3)."""

import os
import tempfile
import unittest

from bookforge import bookdb, policy, shogi
from bookforge.validate import check_result

H1 = "1" * 64
H2 = "2" * 64
AFTER_76 = "lnsgkgsnl/1r5b1/ppppppppp/9/9/2P6/PP1PPPPPP/1B5R1/LNSGKGSNL w -"


class TestShogi(unittest.TestCase):
    def test_normalize_drops_move_number(self):
        self.assertEqual(shogi.normalize(shogi.STARTPOS + " 1"), shogi.STARTPOS)
        self.assertEqual(
            shogi.normalize("sfen " + shogi.STARTPOS + " 37"), shogi.STARTPOS
        )
        self.assertEqual(shogi.apply_move(shogi.STARTPOS, "7g7f"), AFTER_76)

    def test_rejects_garbage_without_crashing(self):
        for bad in [
            "",
            "garbage",
            "9/9/9/9/9/9/9/9/9 b -",
            shogi.STARTPOS + " x",
            "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL q -",
        ]:
            with self.assertRaises(shogi.PositionError):
                shogi.normalize(bad)
        for bad in ["7g7e", "zz", "7g7f7f", "P*5e", "", None]:
            with self.assertRaises(shogi.PositionError):
                shogi.apply_move(shogi.STARTPOS, bad)

    def test_moves_line(self):
        start, moves = shogi.parse_moves_line("startpos moves 7g7f 3c3d")
        self.assertEqual((start, moves), (shogi.STARTPOS, ["7g7f", "3c3d"]))
        start, moves = shogi.parse_moves_line("7g7f")
        self.assertEqual(shogi.positions_along(start, moves)[-1], AFTER_76)
        start, moves = shogi.parse_moves_line(f"position sfen {AFTER_76} 2 moves 3c3d")
        self.assertEqual(start, AFTER_76)
        self.assertEqual(moves, ["3c3d"])

    def test_transposition_same_key(self):
        a = shogi.positions_along(shogi.STARTPOS, ["7g7f", "3c3d", "2g2f"])[-1]
        b = shogi.positions_along(shogi.STARTPOS, ["2g2f", "3c3d", "7g7f"])[-1]
        self.assertEqual(a, b)

    def test_terminal(self):
        self.assertIsNone(shogi.terminal_state(shogi.STARTPOS))
        # Gote king on 5a, sente gold on 5b backed by a silver on 5c: mated.
        mated = "4k4/4G4/4S4/9/9/9/9/9/4K4 w -"
        self.assertEqual(shogi.terminal_state(mated), "mated")


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = bookdb.BookDb(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def args(self, self_side="gote", max_ply=4):
        return {
            "search": {"budget": 100, "multipv": 4, "mode": "st"},
            "policy": {
                "self_side": self_side,
                "self_eval_diff": 0,
                "opp_eval_diff": 60,
                "max_ply_from_root": max_ply,
                "min_budget_for_expand": 0,
            },
        }

    CANDS = [
        {"move": "7g7f", "score_cp": 50, "score_mate": None, "winrate": None},
        {"move": "2g2f", "score_cp": 40, "score_mate": None, "winrate": None},
        {"move": "5i6h", "score_cp": -10, "score_mate": None, "winrate": None},
        {"move": "1g1f", "score_cp": -30, "score_mate": None, "winrate": None},
    ]

    def test_asymmetric_width(self):
        # startpos is sente to move. With self_side=gote, sente is the
        # opponent: everything within 60cp of the best is expanded.
        root, _ = self.book.upsert_node(shogi.STARTPOS, 0)
        new = policy.expand(
            self.book, self.book.node(root), "nnue", self.CANDS, self.args("gote")
        )
        moves = sorted(r["move"] for r in self.book.children(root))
        self.assertEqual(moves, ["2g2f", "5i6h", "7g7f"])
        self.assertEqual(len(new), 3)
        # With self_side=sente only the best move is expanded.
        b2 = bookdb.BookDb(os.path.join(self.tmp.name, "u.db"))
        root2, _ = b2.upsert_node(shogi.STARTPOS, 0)
        policy.expand(b2, b2.node(root2), "nnue", self.CANDS, self.args("sente"))
        self.assertEqual([r["move"] for r in b2.children(root2)], ["7g7f"])
        b2.close()

    def test_priority_prefers_close_moves_and_short_lines(self):
        root, _ = self.book.upsert_node(shogi.STARTPOS, 0)
        policy.expand(
            self.book, self.book.node(root), "nnue", self.CANDS, self.args("gote")
        )
        prio = {
            r["move"]: self.book.node(r["child_id"])["priority"]
            for r in self.book.children(root)
        }
        self.assertGreater(prio["7g7f"], prio["2g2f"])
        self.assertGreater(prio["2g2f"], prio["5i6h"])
        w = policy.DEFAULT_WEIGHTS
        self.assertGreater(
            policy.priority(1, 0, "nnue", w), policy.priority(5, 0, "nnue", w)
        )

    def test_max_ply(self):
        root, _ = self.book.upsert_node(shogi.STARTPOS, 0)
        self.assertEqual(
            policy.expand(
                self.book,
                self.book.node(root),
                "nnue",
                self.CANDS,
                self.args(max_ply=0),
            ),
            [],
        )

    def test_mate_scores(self):
        root, _ = self.book.upsert_node(shogi.STARTPOS, 0)
        cands = [
            {"move": "7g7f", "score_cp": None, "score_mate": 5, "winrate": None},
            {"move": "2g2f", "score_cp": 3000, "score_mate": None, "winrate": None},
        ]
        policy.expand(
            self.book, self.book.node(root), "nnue", cands, self.args("sente")
        )
        self.assertEqual([r["move"] for r in self.book.children(root)], ["7g7f"])

    def test_select_todo(self):
        b = self.book
        root_prio = policy.priority(0, 0, "nnue", policy.DEFAULT_WEIGHTS)
        root, _ = b.upsert_node(shogi.STARTPOS, 0, root_prio)
        policy.expand(b, b.node(root), "nnue", self.CANDS, self.args("gote"))
        todo = b.select_todo("nnue", limit=10)
        self.assertEqual(len(todo), 4)
        self.assertEqual(todo[0]["id"], root)  # root has the highest priority
        self.assertEqual(len(b.select_todo("nnue", limit=10, ply_max=0)), 1)

    def test_queue_lease_release_complete(self):
        b = self.book
        root_prio = policy.priority(0, 0, "nnue", policy.DEFAULT_WEIGHTS)
        root, _ = b.upsert_node(shogi.STARTPOS, 0, root_prio)
        policy.expand(b, b.node(root), "nnue", self.CANDS, self.args("gote"))
        self.assertEqual(b.fill_queue("r1", "nnue"), 4)
        self.assertEqual(b.fill_queue("r1", "nnue"), 0)  # idempotent

        first = b.lease("r1", "nnue", "t1", 2)
        self.assertEqual([r["id"] for r in first][0], root)  # priority order
        self.assertEqual(b.queue_counts("r1"), {"nnue": {"free": 2, "leased": 2}})
        second = b.lease("r1", "nnue", "t2", 10)
        self.assertEqual(len(second), 2)
        self.assertFalse({r["id"] for r in first} & {r["id"] for r in second})
        self.assertEqual(b.lease("r1", "nnue", "t3", 10), [])

        self.assertEqual(b.release("t2"), 2)
        b.complete("r1", "nnue", [r["id"] for r in first])
        self.assertEqual(b.queue_counts("r1"), {"nnue": {"free": 2, "leased": 0}})

        # a node that got a matching evaluation elsewhere is dropped, not leased
        other = second[0]["id"]
        b.insert_eval(
            other,
            kind="nnue",
            engine_hash="e",
            eval_hash="h",
            budget=10,
            depth=1,
            seldepth=1,
            elapsed_ms=1,
            worker_id="w",
            run_id="r0",
            task_id="t0",
            cands=[{"move": "7g7f", "score_cp": 0}],
        )
        rest = b.lease("r1", "nnue", "t4", 10)
        self.assertEqual([r["id"] for r in rest], [second[1]["id"]])
        b.complete("r1", "nnue", [second[1]["id"]])
        self.assertTrue(b.queue_empty("r1"))

        # other runs' queues are separate
        b.fill_queue("r2", "nnue", ply_max=0, eval_hash="new", min_budget=1)
        self.assertEqual(b.queue_counts("r2"), {"nnue": {"free": 1, "leased": 0}})
        b.clear_queue("r2")
        self.assertTrue(b.queue_empty("r2"))

    def test_edges_record_checks(self):
        b = self.book
        sfen = "4k4/9/9/9/9/9/9/9/4K3R b -"  # sente rook on 1i, gote king on 5a
        node, _ = b.upsert_node(sfen, 0)
        policy.create_child(b, node, sfen, "1i1a", 1, 0.0)  # rook to 1a: check
        policy.create_child(b, node, sfen, "1i1e", 1, 0.0)  # rook to 1e: quiet
        rows = {r["move"]: r["gives_check"] for r in b.query("SELECT * FROM edge")}
        self.assertEqual(rows, {"1i1a": 1, "1i1e": 0})


class TestValidate(unittest.TestCase):
    def result(self, **over):
        r = {
            "pos_id": 1,
            "engine_hash": H1,
            "eval_hash": H2,
            "budget": 1000,
            "multipv": [
                {"move": "7g7f", "score_cp": 40, "pv": "7g7f 3c3d 2g2f"},
                {"move": "2g2f", "score_cp": 35},
            ],
        }
        r.update(over)
        return r

    def check(self, result, kind="nnue"):
        return check_result(
            result,
            sfen=shogi.STARTPOS,
            kind=kind,
            engine_hash=H1,
            eval_hash=H2,
            budget=1000,
            multipv=4,
        )

    def test_ok(self):
        self.assertIsNone(self.check(self.result()))

    def test_rejections(self):
        cases = {
            "engine_hash_mismatch": self.result(engine_hash=H2),
            "eval_hash_mismatch": self.result(eval_hash=H1),
            "budget_mismatch": self.result(budget=999),
            "illegal_move": self.result(multipv=[{"move": "7g7e", "score_cp": 1}]),
            "illegal_pv": self.result(
                multipv=[{"move": "7g7f", "score_cp": 1, "pv": "7g7f 7g7f"}]
            ),
            "pv_mismatch": self.result(
                multipv=[{"move": "7g7f", "score_cp": 1, "pv": "2g2f"}]
            ),
            "duplicate_move": self.result(
                multipv=[
                    {"move": "7g7f", "score_cp": 1},
                    {"move": "7g7f", "score_cp": 0},
                ]
            ),
            "too_many_candidates": self.result(
                multipv=[
                    {"move": m, "score_cp": 0}
                    for m in ["7g7f", "2g2f", "1g1f", "9g9f", "5i6h"]
                ]
            ),
            "winrate_from_nnue": self.result(
                multipv=[{"move": "7g7f", "winrate": 0.5}]
            ),
        }
        for reason, res in cases.items():
            self.assertEqual(self.check(res), reason, reason)
        self.assertEqual(self.check(self.result(), kind="dl"), "cp_from_dl")


class TestMigration(unittest.TestCase):
    def test_v1_book_gains_checks_and_queue(self):
        import json
        import sqlite3

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            c = sqlite3.connect(path)
            c.executescript(
                """
                CREATE TABLE node (id INTEGER PRIMARY KEY, sfen TEXT NOT NULL UNIQUE,
                  ply_min INTEGER, status INTEGER NOT NULL, priority REAL NOT NULL
                  DEFAULT 0, value_nnue INTEGER, value_dl REAL, dirty INTEGER NOT NULL
                  DEFAULT 0, updated_at INTEGER);
                CREATE TABLE edge (parent_id INTEGER NOT NULL, move TEXT NOT NULL,
                  child_id INTEGER NOT NULL, PRIMARY KEY (parent_id, move));
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                """
            )
            parent = "4k4/9/9/9/9/9/9/9/4K3R b -"
            c.executemany(
                "INSERT INTO node(id, sfen, ply_min, status) VALUES(?, ?, ?, 2)",
                [
                    (1, parent, 0),
                    (2, shogi.apply_move(parent, "1i1a"), 1),
                    (3, shogi.apply_move(parent, "1i1e"), 1),
                ],
            )
            c.executemany(
                "INSERT INTO edge VALUES(1, ?, ?)", [("1i1a", 2), ("1i1e", 3)]
            )
            c.execute("INSERT INTO meta VALUES('schema_version', ?)", (json.dumps(1),))
            c.commit()
            c.close()

            b = bookdb.BookDb(path)
            self.assertEqual(b.get_meta("schema_version"), bookdb.SCHEMA_VERSION)
            rows = {r["move"]: r["gives_check"] for r in b.query("SELECT * FROM edge")}
            self.assertEqual(rows, {"1i1a": 1, "1i1e": 0})
            self.assertTrue(b.queue_empty("any"))
            b.close()


if __name__ == "__main__":
    unittest.main()
