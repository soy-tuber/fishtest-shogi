"""Propagation on small hand-built graphs (design doc §6, Phase 1 acceptance:
"伝播値が手計算と一致する小さな DAG のテスト")."""

import os
import tempfile
import unittest

from bookforge import bookdb, propagate
from bookforge.values import MATE_CP


class TestSolve(unittest.TestCase):
    """The pure solver, cp values."""

    def test_leaf_uses_own_best(self):
        vals = propagate.solve("nnue", [1], {1: {"a": 30, "b": 50}}, {})
        self.assertEqual(vals[1], 50)

    def test_child_value_replaces_parent_score_for_that_move(self):
        # R: a=+50, b=+30 (R's own MultiPV). A (after a) is evaluated at +20 for
        # its side to move, so a is worth -20 to R; b keeps its own +30.
        own = {1: {"a": 50, "b": 30}, 2: {"x": 20}}
        children = {1: [("a", 2)]}
        vals = propagate.solve("nnue", [1, 2], own, children)
        self.assertEqual(vals[2], 20)
        self.assertEqual(vals[1], 30)

    def test_unevaluated_child_falls_back_to_parent_score(self):
        own = {1: {"a": 50, "b": 30}}
        children = {1: [("a", 2), ("b", 3)]}
        vals = propagate.solve("nnue", [1, 2, 3], own, children)
        self.assertIsNone(vals[2])
        self.assertEqual(vals[1], 50)

    def test_three_ply_tree(self):
        #          R
        #     a /     \ b
        #      A       B
        #   c / \ d    | e
        #    C   D     E
        # leaves (side to move at leaf): C=+10, D=-40, E=+25
        # A = max(-10, +40) = 40 ; B = -25 ; R = max(-40, +25) = 25
        own = {
            1: {"a": 0, "b": 0},
            2: {"c": 0, "d": 0},
            3: {"e": 0},
            4: {"z": 10},
            5: {"z": -40},
            6: {"z": 25},
        }
        children = {1: [("a", 2), ("b", 3)], 2: [("c", 4), ("d", 5)], 3: [("e", 6)]}
        vals = propagate.solve("nnue", range(1, 7), own, children)
        self.assertEqual(vals, {1: 25, 2: 40, 3: -25, 4: 10, 5: -40, 6: 25})

    def test_transposition(self):
        # R -a-> A -c-> T ; R -b-> B -d-> T ; T = +60 for its side to move.
        # A = -60, B = -60 (plus own alternatives), R = max(60, 60) = 60
        own = {
            1: {"a": 0, "b": 0},
            2: {"c": 0, "x": -100},
            3: {"d": 0, "y": -80},
            4: {"z": 60},
        }
        children = {1: [("a", 2), ("b", 3)], 2: [("c", 4)], 3: [("d", 4)]}
        vals = propagate.solve("nnue", [1, 2, 3, 4], own, children)
        self.assertEqual(vals[4], 60)
        self.assertEqual(vals[2], -60)
        self.assertEqual(vals[3], -60)
        self.assertEqual(vals[1], 60)

    def test_terminal_mate(self):
        own = {1: {"a": 100, "b": 20}}
        children = {1: [("b", 2)]}
        fixed = {2: -MATE_CP}  # side to move at 2 is mated
        vals = propagate.solve("nnue", [1, 2], own, children, fixed)
        self.assertEqual(vals[1], MATE_CP - 1)  # mate in 1

    def test_mate_distance_grows_by_one_ply_per_level(self):
        # R -a-> A -b-> T, T is mated. A mates in 1, so R is mated in 2.
        own = {1: {"a": 0}, 2: {"b": 0}}
        children = {1: [("a", 2)], 2: [("b", 3)]}
        vals = propagate.solve("nnue", [1, 2, 3], own, children, {3: -MATE_CP})
        self.assertEqual(vals[2], MATE_CP - 1)
        self.assertEqual(vals[1], -(MATE_CP - 2))

    def test_shorter_mate_is_preferred(self):
        # a mates at once; b leads to a position that is mated in 3 plies.
        own = {1: {"a": 0, "b": 0}}
        children = {1: [("a", 2), ("b", 3)]}
        vals = propagate.solve(
            "nnue", [1], own, children, external={2: -MATE_CP, 3: -(MATE_CP - 3)}
        )
        self.assertEqual(vals[1], MATE_CP - 1)

    def test_best_move_reported(self):
        # a looked best (+50) but its child says -20; b (+30, not expanded)
        # becomes the best move.
        own = {1: {"a": 50, "b": 30}, 2: {"x": 20}}
        children = {1: [("a", 2)]}
        best = {}
        propagate.solve("nnue", [1, 2], own, children, best=best)
        self.assertEqual(best, {1: "b", 2: "x"})

    def test_repetition_cycle_is_draw(self):
        # A -m-> B -n-> A. Both sides' only alternatives are bad for them
        # (A: x=-200, B: y=-150), so both prefer to repeat: draw.
        own = {1: {"m": 0, "x": -200}, 2: {"n": 0, "y": -150}}
        children = {1: [("m", 2)], 2: [("n", 1)]}
        vals = propagate.solve("nnue", [1, 2], own, children)
        self.assertEqual(vals, {1: 0, 2: 0})

    def test_cycle_with_better_exit(self):
        # A can leave the cycle with +120: A = 120, and B (whose only move
        # returns to A) = -120.
        own = {1: {"m": 0, "x": 120}, 2: {"n": 0}}
        children = {1: [("m", 2)], 2: [("n", 1)]}
        vals = propagate.solve("nnue", [1, 2], own, children)
        self.assertEqual(vals, {1: 120, 2: -120})

    def test_winrate(self):
        own = {1: {"a": 0.6, "b": 0.55}, 2: {"x": 0.7}}
        children = {1: [("a", 2)]}
        vals = propagate.solve("dl", [1, 2], own, children)
        self.assertAlmostEqual(vals[2], 0.7)
        self.assertAlmostEqual(vals[1], 0.55)  # a is worth 1 - 0.7 = 0.3


class TestPerpetualCheck(unittest.TestCase):
    """連続王手の千日手: the side that checks on every move of the repetition
    loses; ordinary repetitions stay draws."""

    SENTE = {1: True, 2: False, 3: True, 4: False}

    def test_checker_must_deviate(self):
        # A (sente) -m, check-> B (gote) -n-> A. Staying is a loss for A, so A
        # takes its -200 exit, and B is +200.
        own = {1: {"m": 0, "x": -200}, 2: {"n": 0, "y": -150}}
        children = {1: [("m", 2)], 2: [("n", 1)]}
        best = {}
        vals = propagate.solve(
            "nnue",
            [1, 2],
            own,
            children,
            checks={(1, "m")},
            sente=self.SENTE,
            best=best,
        )
        self.assertEqual(vals, {1: -200, 2: 200})
        # m ties with x (B just returns to A), but only x leaves the loop
        self.assertEqual(best[1], "x")

    def test_checker_without_exit_loses(self):
        own = {1: {"m": 0}, 2: {"n": 0}}
        children = {1: [("m", 2)], 2: [("n", 1)]}
        vals = propagate.solve(
            "nnue", [1, 2], own, children, checks={(1, "m")}, sente=self.SENTE
        )
        self.assertLess(vals[1], -(MATE_CP - 1000))
        self.assertGreater(vals[2], MATE_CP - 1000)
        dl = propagate.solve(
            "dl",
            [1, 2],
            {1: {"m": 0.5}, 2: {"n": 0.5}},
            children,
            checks={(1, "m")},
            sente=self.SENTE,
        )
        self.assertEqual(dl, {1: 0.0, 2: 1.0})

    def test_defender_checking_back_does_not_matter(self):
        # Only sente checks on every move; gote's reply may also check.
        own = {1: {"m": 0}, 2: {"n": 0}}
        children = {1: [("m", 2)], 2: [("n", 1)]}
        vals = propagate.solve(
            "nnue",
            [1, 2],
            own,
            children,
            checks={(1, "m"), (2, "n")},
            sente=self.SENTE,
        )
        # both sides check every move: treated as an ordinary repetition
        self.assertEqual(vals, {1: 0, 2: 0})

    def test_loop_with_a_quiet_move_is_a_draw(self):
        # A can repeat through B with a check or through D with a quiet move,
        # so the repetition is not necessarily perpetual check.
        own = {1: {"m": 0, "p": 0, "x": -200}, 2: {"n": 0}, 4: {"q": 0}}
        children = {1: [("m", 2), ("p", 4)], 2: [("n", 1)], 4: [("q", 1)]}
        vals = propagate.solve(
            "nnue", [1, 2, 4], own, children, checks={(1, "m")}, sente=self.SENTE
        )
        self.assertEqual(vals[1], 0)


class TestPropagateBook(unittest.TestCase):
    """The same tree through SQLite, incrementally via dirty flags, with NNUE
    and DL kept apart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = bookdb.BookDb(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def _node(self, name, ply):
        return self.book.upsert_node(f"fake-{name}", ply)[0]

    def _eval(self, node, kind, cands, budget=1000):
        key = "score_cp" if kind == "nnue" else "winrate"
        self.book.insert_eval(
            node,
            kind=kind,
            engine_hash="e",
            eval_hash="h",
            budget=budget,
            depth=1,
            seldepth=1,
            elapsed_ms=1,
            worker_id="w",
            run_id="r",
            task_id="t",
            cands=[{"move": m, key: v} for m, v in cands],
        )

    def test_tree_and_incremental_update(self):
        b = self.book
        R, A, B, C, D, E = (
            self._node(n, p)
            for n, p in [("R", 0), ("A", 1), ("B", 1), ("C", 2), ("D", 2), ("E", 2)]
        )
        for parent, move, child in [
            (R, "a", A),
            (R, "b", B),
            (A, "c", C),
            (A, "d", D),
            (B, "e", E),
        ]:
            b.add_edge(parent, move, child)
        self._eval(R, "nnue", [("a", 0), ("b", 0)])
        self._eval(A, "nnue", [("c", 0), ("d", 0)])
        self._eval(B, "nnue", [("e", 0)])
        self._eval(C, "nnue", [("z", 10)])
        self._eval(D, "nnue", [("z", -40)])
        self._eval(E, "nnue", [("z", 25)])
        self._eval(R, "dl", [("a", 0.5), ("b", 0.52)])

        propagate.propagate_dirty(b)
        value = {n: b.node(n)["value_nnue"] for n in (R, A, B, C, D, E)}
        self.assertEqual(value, {R: 25, A: 40, B: -25, C: 10, D: -40, E: 25})
        self.assertAlmostEqual(b.node(R)["value_dl"], 0.52)
        self.assertIsNone(b.node(A)["value_dl"])
        self.assertEqual(
            b.query_one("SELECT COUNT(*) c FROM node WHERE dirty=1")["c"], 0
        )

        # A deeper search of E (higher budget wins) makes b bad for R, so R
        # falls back to a: R = max(-A, -B) = max(-40, -90) = -40.
        self._eval(E, "nnue", [("z", -90)], budget=5000)
        propagate.propagate_dirty(b)
        self.assertEqual(b.node(E)["value_nnue"], -90)
        self.assertEqual(b.node(B)["value_nnue"], 90)
        self.assertEqual(b.node(R)["value_nnue"], -40)
        self.assertEqual(b.node(A)["value_nnue"], 40)

        # Full recomputation agrees with the incremental one.
        before = {n: b.node(n)["value_nnue"] for n in (R, A, B, C, D, E)}
        propagate.propagate_all(b)
        after = {n: b.node(n)["value_nnue"] for n in (R, A, B, C, D, E)}
        self.assertEqual(before, after)

    def test_cycle_in_book(self):
        b = self.book
        A, B = self._node("A", 0), self._node("B", 1)
        b.add_edge(A, "m", B)
        b.add_edge(B, "n", A)
        self._eval(A, "nnue", [("m", 0), ("x", -200)])
        self._eval(B, "nnue", [("n", 0), ("y", -150)])
        propagate.propagate_dirty(b)
        self.assertEqual(b.node(A)["value_nnue"], 0)
        self.assertEqual(b.node(B)["value_nnue"], 0)

    def test_perpetual_check_in_book(self):
        b = self.book
        A = b.upsert_node("fake-A b -", 0)[0]  # sente to move
        B = b.upsert_node("fake-B w -", 1)[0]  # gote to move
        b.add_edge(A, "m", B, gives_check=True)
        b.add_edge(B, "n", A)
        self._eval(A, "nnue", [("m", 0), ("x", -190)])
        self._eval(B, "nnue", [("n", 0), ("y", -150)])
        unexpanded = []
        propagate.propagate_dirty(b, unexpanded=unexpanded)
        self.assertEqual(b.node(A)["value_nnue"], -190)
        self.assertEqual(b.node(B)["value_nnue"], 190)
        # A's best move is now x, which has no child position yet
        self.assertIn((A, "nnue", "x"), unexpanded)


if __name__ == "__main__":
    unittest.main()
