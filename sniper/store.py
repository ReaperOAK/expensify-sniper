"""SQLite state: which label events we already handled, and how each bid ended.

Replaces the `last_issue_id` single-watermark approach in the old sniper.py, which
silently dropped any second issue labelled inside the same poll window.
"""

import sqlite3
import time
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_events (
    event_id   INTEGER PRIMARY KEY,
    issue      INTEGER NOT NULL,
    labeled_at TEXT    NOT NULL,
    seen_at    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS bids (
    issue        INTEGER PRIMARY KEY,
    labeled_at   TEXT,
    status       TEXT NOT NULL,      -- triaged|skipped|reproduced|drafted|posted|aborted|failed
    reason       TEXT,
    similarity   INTEGER,
    n_existing   INTEGER,
    comment_id   INTEGER,
    comment_url  TEXT,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS http_cache (
    key   TEXT PRIMARY KEY,
    etag  TEXT,
    body  TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def mark_event_seen(conn: sqlite3.Connection, event_id: int, issue: int, labeled_at: str) -> bool:
    """Record a label event. Returns True if this is the first time we've seen it."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO seen_events (event_id, issue, labeled_at, seen_at) VALUES (?, ?, ?, ?)",
        (event_id, issue, labeled_at, time.time()),
    )
    conn.commit()
    return cur.rowcount > 0


def has_bid(conn: sqlite3.Connection, issue: int) -> bool:
    row = conn.execute("SELECT 1 FROM bids WHERE issue = ?", (issue,)).fetchone()
    return row is not None


def record_bid(
    conn: sqlite3.Connection,
    issue: int,
    status: str,
    *,
    reason: str | None = None,
    labeled_at: str | None = None,
    similarity: int | None = None,
    n_existing: int | None = None,
    comment_id: int | None = None,
    comment_url: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO bids (issue, labeled_at, status, reason, similarity, n_existing,
                          comment_id, comment_url, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(issue) DO UPDATE SET
            status      = excluded.status,
            reason      = COALESCE(excluded.reason, bids.reason),
            labeled_at  = COALESCE(excluded.labeled_at, bids.labeled_at),
            similarity  = COALESCE(excluded.similarity, bids.similarity),
            n_existing  = COALESCE(excluded.n_existing, bids.n_existing),
            comment_id  = COALESCE(excluded.comment_id, bids.comment_id),
            comment_url = COALESCE(excluded.comment_url, bids.comment_url),
            updated_at  = excluded.updated_at
        """,
        (issue, labeled_at, status, reason, similarity, n_existing, comment_id, comment_url, time.time()),
    )
    conn.commit()


def get_cache(conn: sqlite3.Connection, key: str) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT etag, body FROM http_cache WHERE key = ?", (key,)).fetchone()
    return (row["etag"], row["body"]) if row else (None, None)


def set_cache(conn: sqlite3.Connection, key: str, etag: str | None, body: str) -> None:
    conn.execute(
        "INSERT INTO http_cache (key, etag, body) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET etag = excluded.etag, body = excluded.body",
        (key, etag, body),
    )
    conn.commit()


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM bids GROUP BY status").fetchall()
    return {row["status"]: row["n"] for row in rows}
