"""Negamax back-propagation over the position graph (design doc §6).

value(n) = max over moves m of n:
    -value(child(m))                if the child has a value,
    n's own MultiPV score for m     otherwise (child missing or unevaluated).

The graph has transpositions and cycles. It is solved per strongly connected
component in reverse topological order: acyclic parts are exact, and inside a
cycle the values are iterated starting from the draw value, so a line that
both sides are content to repeat ends up as a draw (sennichite). The number
of iterations is capped.

NNUE (cp) and DL (win rate) values are propagated independently.
"""

from bookforge import bookdb
from bookforge.values import DRAW, ENGINE_KINDS, VALUE_COLUMN, cand_value, negate, same

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


def solve(
    kind, members, own, children, fixed=None, external=None, max_iter=MAX_SCC_ITERATIONS
):
    """Pure propagation on an in-memory graph.

    :param members: node ids to compute
    :param own: {node: {move: value}} the node's own MultiPV scores
    :param children: {node: [(move, child)]}
    :param fixed: {node: value} terminal nodes
    :param external: {child: value|None} values of children outside ``members``
    :returns: {node: value|None}
    """
    members = set(members)
    fixed = fixed or {}
    external = external or {}
    vals = {}

    def combine(n):
        if n in fixed:
            return fixed[n]
        moves = dict(own.get(n, {}))
        for move, c in children.get(n, ()):
            v = vals.get(c) if c in members else external.get(c)
            if v is not None:
                moves[move] = negate(kind, v)
        best = None
        for v in moves.values():
            if v is not None and (best is None or v > best):
                best = v
        return best

    for comp in _sccs(sorted(members), children):
        cyclic = len(comp) > 1 or any(
            c == comp[0] for _m, c in children.get(comp[0], ())
        )
        if not cyclic:
            vals[comp[0]] = combine(comp[0])
            continue
        for n in comp:
            vals[n] = DRAW[kind]
        comp.sort()
        for _ in range(max_iter):
            changed = False
            for n in comp:
                v = combine(n)
                if not same(kind, v, vals[n]):
                    vals[n] = v
                    changed = True
            if not changed:
                break
    return vals


def _load(book, ids):
    """Load what ``solve`` needs for the node ids in the temp table _prop."""
    c = book.conn
    children = {}
    for r in c.execute(
        "SELECT e.parent_id, e.move, e.child_id FROM edge e "
        "JOIN _prop p ON p.id = e.parent_id"
    ):
        children.setdefault(r["parent_id"], []).append((r["move"], r["child_id"]))

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
    for r in c.execute(
        "SELECT n.id, n.value_nnue, n.value_dl FROM node n JOIN _prop p ON p.id = n.id"
    ):
        current[r["id"]] = r
    return own, children, fixed, external, current


def _propagate(book, ids_sql, ids_params=()):
    with book.transaction() as c:
        c.execute("DROP TABLE IF EXISTS temp._prop")
        c.execute("CREATE TEMP TABLE _prop(id INTEGER PRIMARY KEY)")
        c.execute(f"INSERT INTO _prop(id) {ids_sql}", ids_params)
        ids = {r[0] for r in c.execute("SELECT id FROM _prop")}
        if not ids:
            c.execute("DROP TABLE temp._prop")
            return 0
        own, children, fixed, external, current = _load(book, ids)
        updates = {}
        for k in ENGINE_KINDS:
            vals = solve(k, ids, own[k], children, fixed[k], external[k])
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


def propagate_dirty(book):
    """Recompute the dirty nodes and all their ancestors. Returns the number
    of nodes whose value changed."""
    return _propagate(
        book,
        """
        WITH RECURSIVE anc(id) AS (
          SELECT id FROM node WHERE dirty = 1
          UNION
          SELECT e.parent_id FROM edge e JOIN anc a ON e.child_id = a.id)
        SELECT id FROM anc
        """,
    )


def propagate_all(book):
    return _propagate(book, "SELECT id FROM node")
