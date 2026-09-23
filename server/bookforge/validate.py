"""Server-side acceptance checks for one search result (design doc §7.3).

The server never trusts a worker: hashes and budget must match what the task
asked for, and every candidate move and PV is replayed on the board.
"""

from bookforge import shogi


def check_result(result, *, sfen, kind, engine_hash, eval_hash, budget, multipv):
    """Return None if the result is acceptable, else a short reason string.

    ``result`` has already passed ``schemas.result``.
    """
    if result["engine_hash"] != engine_hash:
        return "engine_hash_mismatch"
    if result["eval_hash"] != eval_hash:
        return "eval_hash_mismatch"
    if result["budget"] != budget:
        return "budget_mismatch"
    cands = result["multipv"]
    if len(cands) > multipv:
        return "too_many_candidates"
    moves = [c["move"] for c in cands]
    if len(set(moves)) != len(moves):
        return "duplicate_move"
    for c in cands:
        if kind == "nnue" and "winrate" in c:
            return "winrate_from_nnue"
        if kind == "dl" and "score_cp" in c:
            return "cp_from_dl"
    try:
        for c in cands:
            if not shogi.check_pv(sfen, [c["move"]]):
                return "illegal_move"
            pv = c.get("pv")
            if pv:
                pv_moves = pv.split()
                if pv_moves[0] != c["move"]:
                    return "pv_mismatch"
                if not shogi.check_pv(sfen, pv_moves):
                    return "illegal_pv"
    except shogi.PositionError:
        return "illegal_move"
    return None
