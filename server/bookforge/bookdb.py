"""Position DAG of one book, stored in ``books/<book_id>.db`` (design doc §3).

One ``BookDb`` object owns one SQLite connection and serializes every access
with a lock, so the server process is the single writer. Callers group writes
with ``with book.transaction():``.
"""

import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

SCHEMA_VERSION = 1

# node.status
PENDING = 0
LEASED = 1  # reserved; leases are tracked per run in the server, not here
EVALUATED = 2
PRUNED = 3
TERMINAL = 4

BOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node (
  id            INTEGER PRIMARY KEY,
  sfen          TEXT NOT NULL UNIQUE,
  ply_min       INTEGER,
  status        INTEGER NOT NULL,
  priority      REAL NOT NULL DEFAULT 0,
  value_nnue    INTEGER,
  value_dl      REAL,
  dirty         INTEGER NOT NULL DEFAULT 0,
  updated_at    INTEGER
);
CREATE INDEX IF NOT EXISTS node_queue ON node(status, priority DESC);
CREATE INDEX IF NOT EXISTS node_dirty ON node(dirty) WHERE dirty = 1;

CREATE TABLE IF NOT EXISTS eval (
  id            INTEGER PRIMARY KEY,
  node_id       INTEGER NOT NULL REFERENCES node(id),
  engine_kind   TEXT NOT NULL,
  engine_hash   TEXT NOT NULL,
  eval_hash     TEXT NOT NULL,
  budget        INTEGER NOT NULL,
  depth         INTEGER, seldepth INTEGER,
  elapsed_ms    INTEGER,
  worker_id     TEXT NOT NULL,
  run_id        TEXT,
  task_id       TEXT NOT NULL,
  verified      INTEGER NOT NULL DEFAULT 0,
  created_at    INTEGER
);
CREATE INDEX IF NOT EXISTS eval_node ON eval(node_id, engine_kind);

CREATE TABLE IF NOT EXISTS cand (
  eval_id       INTEGER NOT NULL REFERENCES eval(id),
  rank          INTEGER NOT NULL,
  move          TEXT NOT NULL,
  score_cp      INTEGER,
  score_mate    INTEGER,
  winrate       REAL,
  pv            TEXT,
  PRIMARY KEY (eval_id, rank)
);

CREATE TABLE IF NOT EXISTS edge (
  parent_id     INTEGER NOT NULL REFERENCES node(id),
  move          TEXT NOT NULL,
  child_id      INTEGER NOT NULL REFERENCES node(id),
  PRIMARY KEY (parent_id, move)
);
CREATE INDEX IF NOT EXISTS edge_child ON edge(child_id);

CREATE TABLE IF NOT EXISTS root (
  node_id       INTEGER PRIMARY KEY REFERENCES node(id),
  label         TEXT,
  source        TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def book_path(books_dir, book_id):
    if not BOOK_ID_RE.match(book_id or ""):
        raise ValueError(f"invalid book_id: {book_id!r}")
    return os.path.join(books_dir, f"{book_id}.db")


class BookDb:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=30
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.lock:
            # executescript() commits on its own; the statements are idempotent.
            self.conn.executescript(_SCHEMA)
            if self.get_meta("schema_version") is None:
                self.set_meta("schema_version", SCHEMA_VERSION)

    def close(self):
        with self.lock:
            self.conn.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            if self.conn.in_transaction:
                # nested use joins the outer transaction
                yield self.conn
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def query(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    # meta

    def get_meta(self, key, default=None):
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return default if row is None else json.loads(row["value"])

    def set_meta(self, key, value):
        with self.transaction() as c:
            c.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # nodes

    def node(self, node_id):
        return self.query_one("SELECT * FROM node WHERE id=?", (node_id,))

    def node_by_sfen(self, sfen):
        return self.query_one("SELECT * FROM node WHERE sfen=?", (sfen,))

    def upsert_node(self, sfen, ply, priority=0.0, status=PENDING, values=None):
        """Create a node or fold ``ply``/``priority`` into an existing one.

        Returns ``(node_id, created)``. ``values`` ({column: value}) is only
        applied to new nodes (terminal positions).
        """
        now = int(time.time())
        with self.transaction() as c:
            row = c.execute(
                "SELECT id, ply_min, priority FROM node WHERE sfen=?", (sfen,)
            ).fetchone()
            if row is not None:
                if (row["ply_min"] is None or ply < row["ply_min"]) or (
                    priority > row["priority"]
                ):
                    c.execute(
                        "UPDATE node SET ply_min=MIN(COALESCE(ply_min, ?), ?), "
                        "priority=MAX(priority, ?), updated_at=? WHERE id=?",
                        (ply, ply, priority, now, row["id"]),
                    )
                return row["id"], False
            values = values or {}
            cur = c.execute(
                "INSERT INTO node(sfen, ply_min, status, priority, value_nnue, "
                "value_dl, dirty, updated_at) VALUES(?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    sfen,
                    ply,
                    status,
                    priority,
                    values.get("value_nnue"),
                    values.get("value_dl"),
                    now,
                ),
            )
            return cur.lastrowid, True

    def add_edge(self, parent_id, move, child_id):
        """Returns True if the edge is new. A new edge makes the parent dirty,
        since the child may already carry a value."""
        with self.transaction() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO edge(parent_id, move, child_id) VALUES(?, ?, ?)",
                (parent_id, move, child_id),
            )
            if cur.rowcount != 1:
                return False
            c.execute("UPDATE node SET dirty=1 WHERE id=?", (parent_id,))
            return True

    def children(self, node_id):
        return self.query(
            "SELECT move, child_id FROM edge WHERE parent_id=? ORDER BY move",
            (node_id,),
        )

    def parents(self, node_id):
        return self.query(
            "SELECT parent_id, move FROM edge WHERE child_id=?", (node_id,)
        )

    def add_root(self, node_id, label=None, source=None):
        """Returns True if the node was not a root yet."""
        with self.transaction() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO root(node_id, label, source) VALUES(?, ?, ?)",
                (node_id, label, source),
            )
            return cur.rowcount == 1

    def roots(self):
        return self.query(
            "SELECT r.node_id, r.label, r.source, n.sfen, n.status, n.value_nnue, "
            "n.value_dl FROM root r JOIN node n ON n.id = r.node_id ORDER BY r.node_id"
        )

    # evaluations

    def insert_eval(
        self,
        node_id,
        *,
        kind,
        engine_hash,
        eval_hash,
        budget,
        depth,
        seldepth,
        elapsed_ms,
        worker_id,
        run_id,
        task_id,
        cands,
    ):
        """Insert one search result with its MultiPV candidates and mark the
        node evaluated and dirty. Returns the eval id."""
        now = int(time.time())
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO eval(node_id, engine_kind, engine_hash, eval_hash, "
                "budget, depth, seldepth, elapsed_ms, worker_id, run_id, task_id, "
                "verified, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)",
                (
                    node_id,
                    kind,
                    engine_hash,
                    eval_hash,
                    budget,
                    depth,
                    seldepth,
                    elapsed_ms,
                    worker_id,
                    run_id,
                    task_id,
                    now,
                ),
            )
            eval_id = cur.lastrowid
            c.executemany(
                "INSERT INTO cand(eval_id, rank, move, score_cp, score_mate, "
                "winrate, pv) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        eval_id,
                        rank,
                        cd["move"],
                        cd.get("score_cp"),
                        cd.get("score_mate"),
                        cd.get("winrate"),
                        cd.get("pv"),
                    )
                    for rank, cd in enumerate(cands, start=1)
                ],
            )
            c.execute(
                "UPDATE node SET status = CASE WHEN status IN (?, ?) THEN ? "
                "ELSE status END, dirty=1, updated_at=? WHERE id=?",
                (PENDING, LEASED, EVALUATED, now, node_id),
            )
            return eval_id

    def best_eval(self, node_id, kind, *, eval_hash=None, min_budget=0):
        """The deepest (then latest) evaluation of ``kind`` for a node."""
        sql = "SELECT * FROM eval WHERE node_id=? AND engine_kind=? AND budget>=?"
        params = [node_id, kind, min_budget]
        if eval_hash is not None:
            sql += " AND eval_hash=?"
            params.append(eval_hash)
        sql += " ORDER BY budget DESC, id DESC LIMIT 1"
        return self.query_one(sql, params)

    def cands(self, eval_id):
        return self.query(
            "SELECT * FROM cand WHERE eval_id=? ORDER BY rank", (eval_id,)
        )

    # work selection

    def select_todo(
        self,
        kind,
        *,
        limit,
        exclude=(),
        ply_min=None,
        ply_max=None,
        eval_hash=None,
        min_budget=0,
    ):
        """Nodes that still need an evaluation of ``kind`` matching
        (``eval_hash``, budget >= ``min_budget``), best priority first."""
        sql = [
            "SELECT id, sfen FROM node n WHERE n.status IN (?, ?, ?)",
        ]
        params = [PENDING, LEASED, EVALUATED]
        if ply_min is not None:
            sql.append("AND n.ply_min >= ?")
            params.append(ply_min)
        if ply_max is not None:
            sql.append("AND n.ply_min <= ?")
            params.append(ply_max)
        sub = (
            "AND NOT EXISTS (SELECT 1 FROM eval e WHERE e.node_id = n.id "
            "AND e.engine_kind = ? AND e.budget >= ?"
        )
        params += [kind, min_budget]
        if eval_hash is not None:
            sub += " AND e.eval_hash = ?"
            params.append(eval_hash)
        sql.append(sub + ")")
        sql.append("ORDER BY n.priority DESC, n.ply_min, n.id LIMIT ?")
        exclude = set(exclude)
        params.append(limit + len(exclude))
        rows = self.query(" ".join(sql), params)
        return [r for r in rows if r["id"] not in exclude][:limit]

    # statistics

    def counts(self):
        rows = self.query("SELECT status, COUNT(*) AS c FROM node GROUP BY status")
        by_status = {r["status"]: r["c"] for r in rows}
        evals = self.query(
            "SELECT engine_kind, COUNT(*) AS c FROM eval GROUP BY engine_kind"
        )
        return {
            "nodes": sum(by_status.values()),
            "pending": by_status.get(PENDING, 0),
            "evaluated": by_status.get(EVALUATED, 0),
            "terminal": by_status.get(TERMINAL, 0),
            "edges": self.query_one("SELECT COUNT(*) AS c FROM edge")["c"],
            "evals": {r["engine_kind"]: r["c"] for r in evals},
        }
