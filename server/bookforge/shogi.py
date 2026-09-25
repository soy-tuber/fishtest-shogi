"""Position handling: normalization, move application and legality checks.

Position identity is always the *normalized* SFEN: ``<board> <turn> <hands>``
without the move number (design doc §3, §13). Every string that reaches
cshogi is syntax-checked first, since cshogi aborts the process on some
malformed input.
"""

import re

import cshogi

STARTPOS = "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b -"

_BOARD_ROW = r"(?:\+?[PLNSGBRKplnsgbrk]|[1-9])+"
_SFEN_RE = re.compile(
    rf"^{_BOARD_ROW}(?:/{_BOARD_ROW}){{8}} [bw] (?:-|(?:[1-9][0-9]?)?[PLNSGBRplnsgbr](?:(?:[1-9][0-9]?)?[PLNSGBRplnsgbr])*)(?: [1-9][0-9]*)?$"
)
_USI_MOVE_RE = re.compile(r"^(?:[1-9][a-i][1-9][a-i]\+?|[PLNSGBR]\*[1-9][a-i])$")


class PositionError(ValueError):
    pass


def is_usi_move(move):
    return isinstance(move, str) and _USI_MOVE_RE.match(move) is not None


def _board(sfen):
    """Return a cshogi.Board for a (possibly numbered) SFEN, or raise PositionError."""
    if not isinstance(sfen, str):
        raise PositionError("sfen must be a string")
    sfen = sfen.strip()
    if sfen.startswith("sfen "):
        sfen = sfen[5:].strip()
    if _SFEN_RE.match(sfen) is None:
        raise PositionError(f"malformed sfen: {sfen!r}")
    board_part = sfen.split(" ", 1)[0]
    if board_part.count("K") != 1 or board_part.count("k") != 1:
        raise PositionError("sfen must contain exactly one king per side")
    try:
        board = cshogi.Board(sfen)
    except Exception as e:
        raise PositionError(f"invalid sfen {sfen!r}: {e}") from None
    return board


def normalize(sfen):
    """Normalized SFEN (no move number) of any SFEN string."""
    return _normalized_of(_board(sfen))


def _normalized_of(board):
    return " ".join(board.sfen().split(" ")[:3])


def side_to_move(sfen):
    """'sente' or 'gote' for a normalized SFEN."""
    return "sente" if sfen.split(" ")[1] == "b" else "gote"


def legal_move(board, move):
    """Return the cshogi move integer if ``move`` is a legal USI move, else 0."""
    if not is_usi_move(move):
        return 0
    m = board.move_from_usi(move)
    if m and board.is_legal(m):
        return m
    return 0


def apply_move(sfen, move):
    """Normalized SFEN after playing ``move`` in ``sfen``. Raises PositionError."""
    board = _board(sfen)
    m = legal_move(board, move)
    if not m:
        raise PositionError(f"illegal move {move!r} in {sfen!r}")
    board.push(m)
    return _normalized_of(board)


def check_pv(sfen, pv_moves):
    """True if every move of the PV is legal when applied in order."""
    board = _board(sfen)
    for move in pv_moves:
        m = legal_move(board, move)
        if not m:
            return False
        board.push(m)
    return True


def terminal_state(sfen):
    """None for an ordinary position, else 'mated' (side to move has no legal
    move) or 'nyugyoku' (side to move can declare a win)."""
    board = _board(sfen)
    if board.is_game_over():
        return "mated"
    if board.is_nyugyoku():
        return "nyugyoku"
    return None


def parse_moves_line(line):
    """Parse ``[startpos|sfen <sfen>] [moves] m1 m2 ...`` into (start_sfen, moves).

    A bare list of moves is taken to start from the initial position.
    """
    tokens = line.split()
    start = STARTPOS
    if tokens and tokens[0] == "position":
        tokens = tokens[1:]
    if tokens and tokens[0] == "startpos":
        tokens = tokens[1:]
    elif tokens and tokens[0] == "sfen":
        if len(tokens) < 4:
            raise PositionError("truncated sfen")
        # board turn hands [movenum]
        n = 5 if len(tokens) > 4 and tokens[4].isdigit() else 4
        start = " ".join(tokens[1:n])
        tokens = tokens[n:]
    if tokens and tokens[0] == "moves":
        tokens = tokens[1:]
    return normalize(start), tokens


def positions_along(start_sfen, moves):
    """List of normalized SFENs: the start and the position after each move."""
    board = _board(start_sfen)
    out = [_normalized_of(board)]
    for move in moves:
        m = legal_move(board, move)
        if not m:
            raise PositionError(f"illegal move {move!r} at ply {len(out)}")
        board.push(m)
        out.append(_normalized_of(board))
    return out
