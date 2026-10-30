"""SQLite storage for sessions, observations, summaries.

Design notes
------------
* One file, WAL mode, no ORM. The schema is small enough to keep in SQL.
* Every observation keeps `source_ref` pointing at the raw event that produced
  it, so the viewer can always show "why do you remember this".
* Full-text search uses SQLite's built-in FTS5 when available; search falls
  back to LIKE so the app still works on stripped-down SQLite builds.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    harness     TEXT NOT NULL,             -- dsh / claude-code / codex / manual
    project     TEXT NOT NULL DEFAULT '',
    cwd         TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    meta        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,             -- decision / bugfix / discovery / todo / fact
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    source_ref  TEXT NOT NULL DEFAULT '',
    importance  REAL NOT NULL DEFAULT 0.5,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id);
CREATE INDEX IF NOT EXISTS idx_obs_kind ON observations(kind);

CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model       TEXT NOT NULL DEFAULT 'deterministic',
    text        TEXT NOT NULL,
    obs_count   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
    obs_id      INTEGER PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    vector      TEXT NOT NULL,             -- json array of floats
    dim         INTEGER NOT NULL
);

"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS obs_fts USING fts5(
    title, body, tags, content='', tokenize='unicode61'
);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS obs_ai AFTER INSERT ON observations BEGIN
  INSERT INTO obs_fts(rowid, title, body, tags)
  VALUES (new.id, new.title, new.body, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS obs_ad AFTER DELETE ON observations BEGIN
  DELETE FROM obs_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS obs_au AFTER UPDATE ON observations BEGIN
  DELETE FROM obs_fts WHERE rowid = old.id;
  INSERT INTO obs_fts(rowid, title, body, tags)
  VALUES (new.id, new.title, new.body, new.tags);
END;
"""




def has_cjk(text: str) -> bool:
    return re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text) is not None


def bigrams(text: str) -> set[str]:
    """Overlapping character bigrams; used for CJK fuzzy matching because FTS5's
    unicode61 tokenizer swallows whole CJK runs as single tokens."""
    chars = re.sub(r"\s+", "", text)
    return {chars[i:i + 2] for i in range(len(chars) - 1)} if len(chars) > 1 else {chars}


def log_fallback(err: Exception) -> None:
    import sys
    print(f"[amem] FTS5 unavailable ({err}); falling back to LIKE search", file=sys.stderr)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Observation:
    kind: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    source_ref: str = ""
    importance: float = 0.5
    id: int | None = None
    session_id: int | None = None
    created_at: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "source_ref": self.source_ref,
            "importance": self.importance,
        }


class Store:
    """Thin sync wrapper around a SQLite connection."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._has_fts = self._try_fts()

    def _try_fts(self) -> bool:
        """Create the FTS5 index if the SQLite build has it. Older system
        pythons ship without FTS5; search then degrades to LIKE."""
        try:
            self.conn.executescript(FTS_SCHEMA + FTS_TRIGGERS)
            # backfill if the table pre-existed with rows
            n = self.conn.execute("SELECT COUNT(*) FROM obs_fts").fetchone()[0]
            total = self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            if n < total:
                self.conn.execute(
                    "INSERT INTO obs_fts(rowid, title, body, tags)"
                    " SELECT id, title, body, tags FROM observations"
                    " WHERE id NOT IN (SELECT rowid FROM obs_fts)"
                )
            self.conn.commit()
            return True
        except sqlite3.OperationalError as e:
            log_fallback(e)
            return False

    # -- lifecycle ------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- sessions -------------------------------------------------------
    def start_session(self, harness: str, project: str = "", cwd: str = "",
                      meta: dict[str, Any] | None = None) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO sessions(harness, project, cwd, started_at, meta)"
                " VALUES (?,?,?,?,?)",
                (harness, project, cwd, utcnow(), json.dumps(meta or {}, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def end_session(self, session_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE sessions SET ended_at=? WHERE id=?", (utcnow(), session_id))

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT s.*, (SELECT COUNT(*) FROM observations o WHERE o.session_id=s.id) AS obs_count
               FROM sessions s ORDER BY s.id DESC LIMIT ?""", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- observations ---------------------------------------------------
    def add_observation(self, session_id: int, obs: Observation) -> int:
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO observations(session_id, kind, title, body, tags,
                                             source_ref, importance, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (session_id, obs.kind, obs.title, obs.body, json.dumps(obs.tags, ensure_ascii=False),
                 obs.source_ref, obs.importance, utcnow()),
            )
            return int(cur.lastrowid)

    def add_many(self, session_id: int, obs_list: Iterable[Observation]) -> list[int]:
        return [self.add_observation(session_id, o) for o in obs_list]

    def get_observation(self, obs_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["tags"] = json.loads(d["tags"] or "[]")
        return d

    def observations_for_session(self, session_id: int, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM observations WHERE session_id=? ORDER BY id LIMIT ?",
            (session_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"] or "[]")
            out.append(d)
        return out

    def recent(self, limit: int = 20, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self.conn.execute(
                "SELECT * FROM observations WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM observations ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"] or "[]")
            out.append(d)
        return out

    # -- search ---------------------------------------------------------
    @staticmethod
    def _fts_query(q: str) -> str:
        """Turn free text into an FTS5 query: quote each token, OR them."""
        tokens = [t for t in "".join(ch if ch.isalnum() else " " for ch in q).split() if t]
        return " OR ".join(f'"{t}"' for t in tokens) or '""'

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Hybrid search.

        ASCII queries go through FTS5 + bm25. Queries containing CJK use a
        Python-side substring/bigram scorer, because the FTS tokenizer can't
        segment Chinese and would produce confident-looking garbage.
        """
        query = query.strip()
        if not query:
            return []
        rows: list[dict[str, Any]]

        if has_cjk(query):
            rows = self._cjk_search(query, limit)
        elif self._has_fts:
            try:
                rows = [dict(r) for r in self.conn.execute(
                    """SELECT o.*, bm25(obs_fts) AS rank FROM obs_fts
                       JOIN observations o ON o.id = obs_fts.rowid
                       WHERE obs_fts MATCH ? ORDER BY rank LIMIT ?""",
                    (self._fts_query(query), limit * 3)).fetchall()]
            except sqlite3.OperationalError:
                rows = self._like_search(query, limit * 3)
            rows = rows[:limit]
        else:
            rows = self._like_search(query, limit)

        for d in rows:
            d["tags"] = json.loads(d["tags"] or "[]")
        return rows

    def _like_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        like = f"%{query}%"
        return [dict(r) for r in self.conn.execute(
            "SELECT *, 0.0 AS rank FROM observations"
            " WHERE title LIKE ? OR body LIKE ? OR tags LIKE ?"
            " ORDER BY importance DESC, id DESC LIMIT ?", (like, like, like, limit)).fetchall()]

    def _cjk_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        q = re.sub(r"\s+", "", query.lower())
        q_bi = bigrams(q)
        scored = []
        for r in self.conn.execute("SELECT * FROM observations").fetchall():
            d = dict(r)
            hay = f"{d['title']} {d['body']} {d['tags']}".lower()
            hay_uni = re.sub(r"\s+", "", hay)
            if q in hay_uni:
                score = 100.0
            else:
                overlap = q_bi & bigrams(hay_uni)
                score = 60.0 * len(overlap) / max(1, len(q_bi))
            if score <= 0:
                continue
            d["rank"] = -score - float(d.get("importance", 0.5))
            d["score"] = round(score, 1)
            scored.append(d)
        scored.sort(key=lambda x: (-x["score"], -float(x["importance"]), -x["id"]))
        return scored[:limit]

    def timeline(self, obs_id: int, before: int = 3, after: int = 3) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM observations
               WHERE id BETWEEN ? AND ? ORDER BY id""",
            (max(1, obs_id - before), obs_id + after),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"] or "[]")
            out.append(d)
        return out

    # -- summaries ------------------------------------------------------
    def set_summary(self, session_id: int, text: str, model: str = "deterministic",
                    obs_count: int = 0) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO summaries(session_id, model, text, obs_count, created_at)"
                " VALUES (?,?,?,?,?)",
                (session_id, model, text, obs_count, utcnow()),
            )
            return int(cur.lastrowid)

    def latest_summary(self, session_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM summaries WHERE session_id=? ORDER BY id DESC LIMIT 1", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def summaries(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM summaries ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- embeddings -----------------------------------------------------
    def put_embedding(self, obs_id: int, vector: list[float], model: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO embeddings(obs_id, model, vector, dim) VALUES (?,?,?,?)",
                (obs_id, model, json.dumps(vector), len(vector)),
            )

    def embeddings(self, model: str | None = None) -> list[tuple[int, list[float]]]:
        if model:
            rows = self.conn.execute(
                "SELECT obs_id, vector FROM embeddings WHERE model=?", (model,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT obs_id, vector FROM embeddings").fetchall()
        return [(int(r["obs_id"]), json.loads(r["vector"])) for r in rows]

    def stats(self) -> dict[str, Any]:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "sessions": one("SELECT COUNT(*) FROM sessions"),
            "observations": one("SELECT COUNT(*) FROM observations"),
            "summaries": one("SELECT COUNT(*) FROM summaries"),
            "embeddings": one("SELECT COUNT(*) FROM embeddings"),
            "fts": self._has_fts,
        }
