"""SQLite state: label events handled, bids placed, bounty candidates ranked,
and how much of each GitHub quota class has been spent this window.

Replaces the `last_issue_id` single-watermark approach in the old sniper.py, which
silently dropped any second issue labelled inside the same poll window.

`bids.issue INTEGER PRIMARY KEY` assumes a single repo. `candidates` is keyed on
(source, repo, number) instead, so a second source cannot collide with it. The
original three tables are untouched: verify.py's state must keep working.
"""

import json
import sqlite3
import time
from pathlib import Path

from . import config
from .models import Candidate

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

CREATE TABLE IF NOT EXISTS candidates (
    source        TEXT    NOT NULL,
    repo          TEXT    NOT NULL,
    number        INTEGER NOT NULL,
    title         TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    state         TEXT    NOT NULL,
    amount_usd    INTEGER,
    comments      INTEGER NOT NULL,
    labels        TEXT    NOT NULL,
    language      TEXT,
    stars         INTEGER,
    score         REAL,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    first_seen_at REAL    NOT NULL,
    last_seen_at  REAL    NOT NULL,
    rejected      TEXT,
    PRIMARY KEY (source, repo, number)
);

CREATE INDEX IF NOT EXISTS candidates_live
    ON candidates (rejected, score DESC);

CREATE TABLE IF NOT EXISTS rate_budget (
    quota  TEXT    NOT NULL,
    window INTEGER NOT NULL,
    spent  INTEGER NOT NULL,
    PRIMARY KEY (quota, window)
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def event_seen(conn: sqlite3.Connection, event_id: int) -> bool:
    """Have we already handled this label event? Read-only -- records nothing.

    mark_event_seen() both checks and commits, which is wrong for a caller that
    might fail after the check: the event would be burnt without ever being
    processed. This lets a source look first and mark only once the work landed.
    """
    row = conn.execute(
        "SELECT 1 FROM seen_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    return row is not None


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


def upsert_candidate(
    conn: sqlite3.Connection,
    cand: Candidate,
    *,
    rejected: str | None = None,
    now: float | None = None,
) -> bool:
    """Record a candidate. Returns True if this is the first time we have seen it.

    first_seen_at is preserved on update: it is our own freshness clock, and the
    only honest one. GitHub's created_at is displayed but never scored, because
    bounty labels land on issues that already exist.

    A rejected candidate is still written, with its reason. Deleting it would
    make "filtered out" and "never discovered" look identical on the next run.
    """
    stamp = time.time() if now is None else now
    source, repo, number = cand.key
    labels = json.dumps(list(cand.labels), ensure_ascii=False)

    # Newness comes from the INSERT itself, not from a preceding SELECT.
    #
    # A bare SELECT takes no lock in Python's sqlite3, so a read-then-write pair
    # lets two processes both conclude "new" for the same key -- measured, 8 of
    # 10 racing writers did. INSERT OR IGNORE resolves that in one statement:
    # exactly one writer gets rowcount 1, everyone else gets 0. hunt.py and the
    # Expensify watcher share this database, so "concurrent" is the normal case,
    # not a corner one.
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO candidates
            (source, repo, number, title, url, state, amount_usd, comments, labels,
             language, stars, score, created_at, updated_at, first_seen_at,
             last_seen_at, rejected)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source, repo, number, cand.title, cand.url, cand.state, cand.amount_usd,
         cand.comments, labels, cand.language, cand.stars, cand.score,
         cand.created_at, cand.updated_at, stamp, stamp, rejected),
    )
    is_new = cur.rowcount > 0
    if not is_new:
        # first_seen_at is deliberately absent from the SET list: it is our own
        # freshness clock and must survive every later sighting.
        conn.execute(
            """
            UPDATE candidates SET
                title = ?, url = ?, state = ?, amount_usd = ?, comments = ?,
                labels = ?, language = ?, stars = ?, score = ?, updated_at = ?,
                last_seen_at = ?, rejected = ?
            WHERE source = ? AND repo = ? AND number = ?
            """,
            (cand.title, cand.url, cand.state, cand.amount_usd, cand.comments,
             labels, cand.language, cand.stars, cand.score, cand.updated_at,
             stamp, rejected, source, repo, number),
        )
    conn.commit()
    return is_new


def known_updated_at(
    conn: sqlite3.Connection, source: str, repo: str, number: int
) -> str | None:
    """The updated_at we last recorded, or None if this candidate is new to us."""
    row = conn.execute(
        "SELECT updated_at FROM candidates WHERE source = ? AND repo = ? AND number = ?",
        (source, repo, number),
    ).fetchone()
    return row["updated_at"] if row else None


def candidate_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]


def live_candidates(
    conn: sqlite3.Connection, limit: int, *, since: float | None = None
) -> list[sqlite3.Row]:
    """Unrejected candidates, best first. `since` restricts to newly first-seen ones."""
    if since is None:
        return conn.execute(
            "SELECT * FROM candidates WHERE rejected IS NULL "
            "ORDER BY score DESC, amount_usd DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM candidates WHERE rejected IS NULL AND first_seen_at >= ? "
        "ORDER BY score DESC, amount_usd DESC LIMIT ?",
        (since, limit),
    ).fetchall()


# Measured 2026-08-05 against the live API. Search and core are separate pools
# with separate windows; spending one does not touch the other.
QUOTA_LIMIT = {"search": config.SEARCH_QUOTA_PER_MIN, "core": config.CORE_QUOTA_PER_HOUR}
WINDOW_SECONDS = {"search": 60, "core": 3600}


def _window(quota: str, now: float | None) -> int:
    stamp = time.time() if now is None else now
    return int(stamp // WINDOW_SECONDS[quota])


def reserve(
    conn: sqlite3.Connection, quota: str, n: int = 1, *, now: float | None = None
) -> bool:
    """Atomically claim `n` units of `quota` in the current window.

    Returns False and spends nothing if the claim would exceed the limit. The
    guard lives in SQLite rather than in a process because hunt.py and the
    Expensify watcher are separate processes sharing one token.
    """
    if quota not in QUOTA_LIMIT:
        raise ValueError(f"unknown quota {quota!r}; known: {sorted(QUOTA_LIMIT)}")
    window = _window(quota, now)
    conn.execute(
        "INSERT OR IGNORE INTO rate_budget (quota, window, spent) VALUES (?, ?, 0)",
        (quota, window),
    )
    cur = conn.execute(
        "UPDATE rate_budget SET spent = spent + ? "
        "WHERE quota = ? AND window = ? AND spent + ? <= ?",
        (n, quota, window, n, QUOTA_LIMIT[quota]),
    )
    conn.commit()
    return cur.rowcount > 0


def spent(conn: sqlite3.Connection, quota: str, *, now: float | None = None) -> int:
    row = conn.execute(
        "SELECT spent FROM rate_budget WHERE quota = ? AND window = ?",
        (quota, _window(quota, now)),
    ).fetchone()
    return row["spent"] if row else 0


def reserve_or_wait(
    conn: sqlite3.Connection,
    quota: str,
    n: int = 1,
    *,
    max_wait_s: float | None = None,
    clock=None,
    sleep=time.sleep,
) -> bool:
    """Claim `n` units, sleeping to the next window if this one is exhausted.

    `clock` is a CALLABLE returning epoch seconds -- unlike reserve()/spent(),
    whose `now` is a plain float -- because this function reads the time again
    after every sleep.

    Returns False when `max_wait_s` elapses without a slot -- a TRANSIENT
    condition. Callers turn that into a visible `⚠` line in the digest, because
    silently returning fewer results is the failure mode trap #3 describes.

    Asking for more than a whole window holds can never succeed, so it RAISES
    instead of returning False. Returning False there would disguise a caller
    bug as ordinary throttling forever -- the same "error looks like absent"
    trap this guard exists to prevent.
    """
    if quota not in QUOTA_LIMIT:
        raise ValueError(f"unknown quota {quota!r}; known: {sorted(QUOTA_LIMIT)}")
    if n > QUOTA_LIMIT[quota]:
        raise ValueError(
            f"cannot reserve {n} units of {quota}: a whole window holds only "
            f"{QUOTA_LIMIT[quota]}, so this can never succeed")
    read_clock = (lambda: time.time()) if clock is None else clock
    limit = config.BUDGET_WAIT_S if max_wait_s is None else max_wait_s
    waited = 0.0
    while True:
        if reserve(conn, quota, n, now=read_clock()):
            return True
        if waited >= limit:
            return False
        window_s = WINDOW_SECONDS[quota]
        remaining = window_s - (read_clock() % window_s)
        sleep(remaining)
        waited += remaining


def prune_budget(conn: sqlite3.Connection, *, now: float | None = None) -> None:
    """Drop windows that can no longer be spent against."""
    for quota in QUOTA_LIMIT:
        conn.execute(
            "DELETE FROM rate_budget WHERE quota = ? AND window < ?",
            (quota, _window(quota, now)),
        )
    conn.commit()
