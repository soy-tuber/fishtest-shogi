"""Expansion policy and priorities (design doc §5).

All decisions about which positions to create and in which order to search
them live here, on the server. Workers never decide.
"""

import math

from bookforge import bookdb, shogi
from bookforge.values import VALUE_COLUMN, cand_value, terminal_value

# §5.2 default weights. ``tau`` is the closeness scale in the unit of the
# engine kind (cp for NNUE, win rate for DL).
DEFAULT_WEIGHTS = {
    "w_line": 1.0,
    "w_close": 1.0,
    "w_freq": 0.0,
    "w_split": 2.0,
    "w_unst": 1.0,
    "tau_nnue": 50.0,
    "tau_dl": 0.03,
}

# Default width of the expansion window for DL runs (win rate units). The
# cp-based ``self_eval_diff`` / ``opp_eval_diff`` are never converted.
DEFAULT_DL_DIFF = {"self_winrate_diff": 0.0, "opp_winrate_diff": 0.05}


def weights_for(run_args):
    w = dict(DEFAULT_WEIGHTS)
    w.update(run_args.get("weights", {}))
    return w


def priority(ply, delta, kind, weights, freq=0, split=False, unstable=False):
    """§5.2. ``delta`` = |best - this move| in the parent, in ``kind`` units."""
    tau = weights[f"tau_{kind}"]
    return (
        weights["w_line"] / (1 + ply)
        + weights["w_close"] * math.exp(-abs(delta) / tau)
        + weights["w_freq"] * math.log1p(freq)
        + weights["w_split"] * (1.0 if split else 0.0)
        + weights["w_unst"] * (1.0 if unstable else 0.0)
    )


def expansion_width(policy, kind, turn):
    """Allowed distance from the best move for the side to move ``turn``."""
    is_self = turn == policy["self_side"]
    if kind == "nnue":
        return policy["self_eval_diff"] if is_self else policy["opp_eval_diff"]
    key = "self_winrate_diff" if is_self else "opp_winrate_diff"
    return policy.get(key, DEFAULT_DL_DIFF[key])


def select_moves(policy, kind, turn, cands):
    """Moves of a MultiPV list inside the expansion window, with their
    distance to the best move. ``cands`` are dicts/rows with score fields."""
    scored = [(c["move"], cand_value(kind, c)) for c in cands]
    scored = [(m, v) for m, v in scored if v is not None]
    if not scored:
        return []
    best = max(v for _m, v in scored)
    width = expansion_width(policy, kind, turn)
    return [(m, best - v) for m, v in scored if best - v <= width]


def create_child(book, parent_id, parent_sfen, move, ply, prio):
    """Create (or reuse) the child reached by ``move`` and link it.

    Returns ``(child_id, created)``.
    """
    child_sfen = shogi.apply_move(parent_sfen, move)
    state = shogi.terminal_state(child_sfen)
    if state is None:
        child_id, created = book.upsert_node(child_sfen, ply, prio)
    else:
        values = {VALUE_COLUMN[k]: terminal_value(k, state) for k in VALUE_COLUMN}
        child_id, created = book.upsert_node(
            child_sfen, ply, prio, status=bookdb.TERMINAL, values=values
        )
    book.add_edge(parent_id, move, child_id)
    return child_id, created


def expand(book, node, kind, cands, run_args):
    """§5.1: create the children of an evaluated node. Returns the new node ids."""
    policy = run_args["policy"]
    budget = run_args["search"]["budget"]
    ply = node["ply_min"] or 0
    if ply + 1 > policy["max_ply_from_root"]:
        return []
    if budget < policy["min_budget_for_expand"]:
        return []
    weights = weights_for(run_args)
    turn = shogi.side_to_move(node["sfen"])
    created = []
    with book.transaction():
        for move, delta in select_moves(policy, kind, turn, cands):
            prio = priority(ply + 1, delta, kind, weights)
            child_id, new = create_child(
                book, node["id"], node["sfen"], move, ply + 1, prio
            )
            if new:
                created.append(child_id)
    return created
