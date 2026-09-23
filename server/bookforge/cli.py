"""Admin command line for Phase 1 (until the screens of Phase 5 exist).

    uv run python -m bookforge.cli create-book gote-76fu-34fu --self-side gote
    uv run python -m bookforge.cli add-roots gote-76fu-34fu --moves "7g7f 3c3d"
    uv run python -m bookforge.cli add-roots gote-76fu-34fu --sfen-file roots.txt
    uv run python -m bookforge.cli create-run run.json --approve
    uv run python -m bookforge.cli approve <run_id>
    uv run python -m bookforge.cli status <run_id>
    uv run python -m bookforge.cli propagate gote-76fu-34fu

Uses the same MongoDB (``BOOKFORGE_DB``) and books directory
(``BOOKFORGE_BOOKS_DIR``) as the server. Run it on the coordinator host.
"""

import argparse
import json
import os
import sys

from bookforge import propagate
from bookforge.service import Bookforge, BookforgeError


def _service():
    from pymongo import MongoClient

    db = MongoClient("localhost")[os.environ.get("BOOKFORGE_DB", "bookforge")]
    return Bookforge(db, os.environ.get("BOOKFORGE_BOOKS_DIR", "books"))


def main(argv=None, service=None):
    p = argparse.ArgumentParser(prog="bookforge.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("create-book")
    s.add_argument("book_id")
    s.add_argument("--self-side", choices=["sente", "gote"], required=True)
    s.add_argument("--description", default="")

    s = sub.add_parser("add-roots")
    s.add_argument("book_id")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--sfen", action="append", help="one SFEN (repeatable)")
    g.add_argument("--sfen-file", help="file with one SFEN per line")
    g.add_argument(
        "--moves",
        action="append",
        help='move sequence from startpos, e.g. "7g7f 3c3d" (repeatable)',
    )
    g.add_argument("--moves-file", help="file with one move sequence per line")
    s.add_argument("--label")

    s = sub.add_parser("create-run")
    s.add_argument("args_json", help="run configuration (design doc §4)")
    s.add_argument("--user", default=os.environ.get("USER", "admin"))
    s.add_argument("--approve", action="store_true")

    for name in ("approve", "pause", "resume", "status"):
        s = sub.add_parser(name)
        s.add_argument("run_id")

    s = sub.add_parser("list-runs")

    s = sub.add_parser("propagate")
    s.add_argument("book_id")
    s.add_argument("--all", action="store_true", help="full recomputation")

    a = p.parse_args(argv)
    bf = service or _service()
    try:
        if a.cmd == "create-book":
            meta = {"self_side": a.self_side}
            if a.description:
                meta["description"] = a.description
            bf.create_book(a.book_id, meta)
            out = {"book_id": a.book_id}
        elif a.cmd == "add-roots":
            if a.sfen or a.sfen_file:
                source = "sfen"
                lines = (
                    a.sfen or open(a.sfen_file, encoding="utf-8").read().splitlines()
                )
            else:
                source = "moves"
                lines = (
                    a.moves or open(a.moves_file, encoding="utf-8").read().splitlines()
                )
            out = bf.add_roots(a.book_id, lines, source=source, label=a.label)
        elif a.cmd == "create-run":
            with open(a.args_json, encoding="utf-8") as f:
                args = json.load(f)
            run_id = bf.create_run(
                args, username=a.user, approved=a.approve, approver=a.user
            )
            out = {"run_id": run_id, "state": bf.get_run(run_id)["state"]}
        elif a.cmd == "approve":
            bf.approve_run(a.run_id, os.environ.get("USER", "admin"))
            out = {"run_id": a.run_id, "state": "active"}
        elif a.cmd in ("pause", "resume"):
            bf.pause_run(a.run_id, paused=a.cmd == "pause")
            out = {"run_id": a.run_id, "state": bf.get_run(a.run_id)["state"]}
        elif a.cmd == "status":
            out = bf.run_status(a.run_id)
        elif a.cmd == "list-runs":
            out = [
                {
                    "run_id": str(r["_id"]),
                    "type": r["args"]["type"],
                    "book_id": r["args"]["book_id"],
                    "state": r["state"],
                    "positions": r["results"]["positions"],
                }
                for r in bf.runs.find(
                    {},
                    {
                        "args.type": 1,
                        "args.book_id": 1,
                        "state": 1,
                        "results.positions": 1,
                    },
                )
            ]
        elif a.cmd == "propagate":
            b = bf.book(a.book_id)
            fn = propagate.propagate_all if a.all else propagate.propagate_dirty
            out = {"changed": fn(b)}
    except (BookforgeError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
