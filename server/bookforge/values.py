"""Value conventions per engine kind (design doc §0, §6).

NNUE values are centipawns from the side to move, DL values are win rates
from the side to move. The two are never mixed or converted into each other:
every function here takes the engine kind and stays inside that unit.
"""

import math

ENGINE_KINDS = ("nnue", "dl")

# Same magnitude as YaneuraOu's VALUE_MATE; "mate in n plies" maps to
# MATE_CP - n so that shorter mates are preferred.
MATE_CP = 32000

VALUE_COLUMN = {"nnue": "value_nnue", "dl": "value_dl"}
DRAW = {"nnue": 0, "dl": 0.5}
WIN = {"nnue": MATE_CP, "dl": 1.0}
LOSS = {"nnue": -MATE_CP, "dl": 0.0}


def negate(kind, value):
    """Value seen from the other side."""
    if value is None:
        return None
    return -value if kind == "nnue" else 1.0 - value


def mate_to_cp(mate):
    """USI ``score mate n`` (n plies, sign = who mates) to centipawns."""
    if mate > 0:
        return MATE_CP - mate
    return -MATE_CP - mate  # mate <= 0: -(MATE_CP - |mate|)


def _field(cand, key):
    # worker dicts omit absent scores, sqlite Rows carry them as NULL
    try:
        return cand[key]
    except KeyError, IndexError:
        return None


def cand_value(kind, cand):
    """Value of one MultiPV candidate (a dict or sqlite Row with score fields)."""
    mate = _field(cand, "score_mate")
    if kind == "nnue":
        if mate is not None:
            return mate_to_cp(mate)
        return _field(cand, "score_cp")
    winrate = _field(cand, "winrate")
    if winrate is not None:
        return winrate
    if mate is not None:
        return 1.0 if mate > 0 else 0.0
    return None


def same(kind, a, b):
    if a is None or b is None:
        return a is b
    if kind == "nnue":
        return a == b
    return math.isclose(a, b, abs_tol=1e-9)


def terminal_value(kind, state):
    """Value for the side to move of a terminal position."""
    if state == "mated":
        return LOSS[kind]
    if state == "nyugyoku":
        return WIN[kind]
    raise ValueError(state)
