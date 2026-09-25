"""Negamax back-propagation over the position graph (design doc §6).

value(n) = max over moves m of n:
    -value(child(m))                if the child has a value,
    n's own MultiPV score for m     otherwise (child missing or unevaluated).

Mate scores move one ply further away at each level (``values.backed_up``).

The graph has transpositions and cycles. It is solved per strongly connected
component in reverse topological order: acyclic parts are exact, and inside a
cycle the values are iterated from a start value, so a line both sides are
content to repeat ends up at that value. The start value is a draw
(sennichite), except when every move one side makes inside the cycle gives
check: then any repetition is perpetual check (連続王手の千日手), which that
side loses. A cycle where the checking side also has quiet moves inside it is
treated as a draw. The number of iterations is capped.

NNUE (cp) and DL (win rate) values are propagated independently.
"""

from bookforge import bookdb
from bookforge.values import (
    DRAW,
    ENGINE_KINDS,
    LOSS,
    VALUE_COLUMN,
    WIN,
    backed_up,
    cand_value,
    same,
)

MAX_SCC_ITERATIONS = 64


def _sccs(members, children):
    """Tarjan's algorithm, iterative. Returns SCCs (lists) sinks first.
    ``members`` gives the visiting order; membership tests use a set."""
    member_set = set(members)
    index = {}
    low = {}
    on_stack = set()
    stack = []
    counter = 0
    out = []
    for start in members:
        if start in index:
            continue
        work = [(start, iter(children.get(start, ())))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            v, it = work[-1]
            advanced = False
            for _move, w in it:
                if w not in member_set:
                    continue
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(children.get(w, ()))))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                u = work[-1][0]
                low[u] = min(low[u], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(comp)
    return out


def _perpetual_checker(comp, children, checks, sente):
    """The side (True = sente) all of whose moves inside the cycle ``comp``
    give check, or None."""
    if checks is None or sente is None:
        return None
    members = set(comp)
    found = []
    for side in (True, False):
        side_nodes = [n for n in comp if sente.get(n) == side]
        intra = [
            (n, m) for n in side_nodes for m, c in children.get(n, ()) if c in members
        ]
        if intra and all(e in checks for e in intra):
            found.append(side)
    return found[0] if len(found) == 1 else None


def solve(
    kind,
    members,
    own,
    children,
    fixed=None,
    external=None,
    *,
    checks=None,
    sente=None,
    best=None,
    max_iter=MAX_SCC_ITERATIONS,
):
    """Pure propagation on an in-memory graph.

    :param members: node ids to compute
    :param own: {node: {move: value}} the node's own MultiPV scores
    :param children: {node: [(move, child)]}
    :param fixed: {node: value} terminal nodes
    :param external: {child: value|None} values of children outside ``members``
    :param checks: set of (node, move) edges whose move gives check
    :param sente: {node: True if sente to move}
    :param best: if a dict, filled with {node: best move}
    :returns: {node: value|None}
    """
    members = set(members)
    fixed = fixed or {}
    external = external or {}
    vals = {}
    best_move = {}

    def combine(n, loop=frozenset()):
        """``loop``: the cycle n is in. On equal values a move that leaves
        the cycle is preferred, since repeating is only worth a draw (or a
        loss under perpetual check), not the value it ties with."""
        if n in fixed:
            return fixed[n]
        moves = {m: (v, False) for m, v in own.get(n, {}).items()}
        for move, c in children.get(n, ()):
            v = vals.get(c) if c in members else external.get(c)
            if v is not None:
                moves[move] = (backed_up(kind, v), c in loop)
        top, top_move, top_loops = None, None, False
        for move, (v, loops) in moves.items():
            if v is None:
                continue
            if (
                top is None
                or (v > top and not same(kind, v, top))
                or (same(kind, v, top) and top_loops and not loops)
            ):
                top, top_move, top_loops = v, move, loops
        best_move[n] = top_move
        return top

    for comp in _sccs(sorted(members), children):
        cyclic = len(comp) > 1 or any(
            c == comp[0] for _m, c in children.get(comp[0], ())
        )
        if not cyclic:
            vals[comp[0]] = combine(comp[0])
            continue
        checker = _perpetual_checker(comp, children, checks, sente)
        for n in comp:
            if checker is None:
                vals[n] = DRAW[kind]
            else:
                vals[n] = LOSS[kind] if sente[n] == checker else WIN[kind]
        comp.sort()
        loop = frozenset(comp)
        for _ in range(max_iter):
            changed = False
            for n in comp:
                v = combine(n, loop)
                if not same(kind, v, vals[n]):
                    vals[n] = v
                    changed = True
            if not changed:
                break
    if best is not None:
        best.update((n, m) for n, m in best_move.items() if m is not None)
    return vals


def _load(book, ids):
    """Load what ``solve`` needs for the node ids in the temp table _prop."""
    c = book.conn
    children = {}
    checks = set()
    for r in c.execute(
        "SELECT e.parent_id, e.move, e.child_id, e.gives_check FROM edge e "
        "JOIN _prop p ON p.id = e.parent_id"
    ):
        children.setdefault(r["parent_id"], []).append((r["move"], r["child_id"]))
        if r["gives_check"]:
            checks.add((r["parent_id"], r["move"]))

    outside = {cid for lst in children.values() for _m, cid in lst} - ids
    fixed = {k: {} for k in ENGINE_KINDS}
    for r in c.execute(
        "SELECT n.id, n.value_nnue, n.value_dl FROM node n "
        "JOIN _prop p ON p.id = n.id WHERE n.status = ?",
        (bookdb.TERMINAL,),
    ):
        for k in ENGINE_KINDS:
            fixed[k][r["id"]] = r[VALUE_COLUMN[k]]

    external = {k: {} for k in ENGINE_KINDS}
    for cid in outside:
        r = c.execute(
            "SELECT value_nnue, value_dl FROM node WHERE id=?", (cid,)
        ).fetchone()
        for k in ENGINE_KINDS:
            external[k][cid] = r[VALUE_COLUMN[k]]

    own = {k: {} for k in ENGINE_KINDS}
    for k in ENGINE_KINDS:
        rows = c.execute(
            """
            WITH best AS (
              SELECT e.id, e.node_id, ROW_NUMBER() OVER (
                PARTITION BY e.node_id ORDER BY e.budget DESC, e.id DESC) AS rn
              FROM eval e JOIN _prop p ON p.id = e.node_id
              WHERE e.engine_kind = ?)
            SELECT b.node_id, c.move, c.score_cp, c.score_mate, c.winrate
            FROM best b JOIN cand c ON c.eval_id = b.id WHERE b.rn = 1
            """,
            (k,),
        )
        for r in rows:
            own[k].setdefault(r["node_id"], {})[r["move"]] = cand_value(k, r)

    current = {}
    sente = {}
    for r in c.execute(
        "SELECT n.id, n.value_nnue, n.value_dl, instr(n.sfen, ' b ') > 0 AS sente "
        "FROM node n JOIN _prop p ON p.id = n.id"
    ):
        current[r["id"]] = r
        sente[r["id"]] = bool(r["sente"])
    return own, children, checks, sente, fixed, external, current


def _propagate(book, ids_sql, ids_params=(), unexpanded=None):
    with book.transaction() as c:
        c.execute("DROP TABLE IF EXISTS temp._prop")
        c.execute("CREATE TEMP TABLE _prop(id INTEGER PRIMARY KEY)")
        c.execute(f"INSERT INTO _prop(id) {ids_sql}", ids_params)
        ids = {r[0] for r in c.execute("SELECT id FROM _prop")}
        if not ids:
            c.execute("DROP TABLE temp._prop")
            return 0
        own, children, checks, sente, fixed, external, current = _load(book, ids)
        updates = {}
        for k in ENGINE_KINDS:
            best = {}
            vals = solve(
                k,
                ids,
                own[k],
                children,
                fixed[k],
                external[k],
                checks=checks,
                sente=sente,
                best=best,
            )
            if unexpanded is not None:
                for n, move in best.items():
                    if n in own[k] and move not in {m for m, _c in children.get(n, ())}:
                        unexpanded.append((n, k, move))
            col = VALUE_COLUMN[k]
            for n, v in vals.items():
                if not same(k, v, current[n][col]):
                    updates.setdefault(n, {})[col] = v
        for n, cols in updates.items():
            sets = ", ".join(f"{col}=?" for col in cols)
            c.execute(f"UPDATE node SET {sets} WHERE id=?", (*cols.values(), n))
        c.execute("UPDATE node SET dirty=0 WHERE id IN (SELECT id FROM _prop)")
        c.execute("DROP TABLE temp._prop")
        return len(updates)


def propagate_dirty(book, unexpanded=None):
    """Recompute the dirty nodes and all their ancestors. Returns the number
    of nodes whose value changed.

    If ``unexpanded`` is a list, (node, kind, move) is appended for every
    recomputed node whose best move has no child position yet."""
    return _propagate(
        book,
        """
        WITH RECURSIVE anc(id) AS (
          SELECT id FROM node WHERE dirty = 1
          UNION
          SELECT e.parent_id FROM edge e JOIN anc a ON e.child_id = a.id)
        SELECT id FROM anc
        """,
        unexpanded=unexpanded,
    )


def propagate_all(book, unexpanded=None):
    return _propagate(book, "SELECT id FROM node", unexpanded=unexpanded)
