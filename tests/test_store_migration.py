"""Adding a column to an existing database.

CREATE TABLE IF NOT EXISTS is not a migration. Every deployed database was
created from an older SCHEMA string and will keep its old shape forever, so a
new column has to be added explicitly -- and the failure mode without it is not
a crash on startup but an OperationalError deep inside a 3-hourly cron run,
after the sweep has already spent its quota.
"""

import sqlite3

from sniper import store

OLD_SCHEMA = """
CREATE TABLE candidates (
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
"""


def test_connect_adds_the_column_to_a_database_made_from_the_old_schema(tmp_path):
    db = tmp_path / "old.sqlite"
    raw = sqlite3.connect(db)
    raw.executescript(OLD_SCHEMA)
    raw.execute(
        "INSERT INTO candidates VALUES "
        "('github-labels','acme/w',1,'t','u','open',100,0,'[]',NULL,NULL,NULL,"
        "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',1.0,1.0,NULL)")
    raw.commit()
    raw.close()

    conn = store.connect(db)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)")}
    assert "open_pr_count" in columns


def test_migration_preserves_the_rows_already_there(tmp_path):
    db = tmp_path / "old.sqlite"
    raw = sqlite3.connect(db)
    raw.executescript(OLD_SCHEMA)
    raw.execute(
        "INSERT INTO candidates VALUES "
        "('github-labels','acme/w',1,'t','u','open',100,0,'[]',NULL,NULL,NULL,"
        "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',1.0,1.0,NULL)")
    raw.commit()
    raw.close()

    conn = store.connect(db)
    row = conn.execute("SELECT * FROM candidates").fetchone()
    assert row["repo"] == "acme/w"
    assert row["amount_usd"] == 100
    assert row["open_pr_count"] is None


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "fresh.sqlite"
    store.connect(db).close()
    conn = store.connect(db)  # second open must not raise "duplicate column name"
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)")}
    assert "open_pr_count" in columns


def test_reopening_a_database_that_actually_ran_the_alter_is_idempotent(tmp_path):
    """test_migration_is_idempotent above only reopens a database born from
    SCHEMA, which never goes through ALTER TABLE at all -- it is already on the
    new shape from CREATE TABLE IF NOT EXISTS. That leaves the actually risky
    reopen unexercised: a database that took the OLD_SCHEMA path, ran the ALTER
    once, and is then opened again. In production that second connect() is
    hunt.py's cron or the Expensify watch sweep starting up minutes later
    against the now-migrated file -- exactly the case _migrate's duplicate-
    column guard exists for.
    """
    db = tmp_path / "old.sqlite"
    raw = sqlite3.connect(db)
    raw.executescript(OLD_SCHEMA)
    raw.execute(
        "INSERT INTO candidates VALUES "
        "('github-labels','acme/w',1,'t','u','open',100,0,'[]',NULL,NULL,NULL,"
        "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',1.0,1.0,NULL)")
    raw.commit()
    raw.close()

    store.connect(db).close()  # first open: runs the ALTER
    conn = store.connect(db)   # second open: must not raise "duplicate column name"

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)")}
    assert "open_pr_count" in columns
    row = conn.execute("SELECT * FROM candidates").fetchone()
    assert row["repo"] == "acme/w"
    assert row["amount_usd"] == 100
    assert row["open_pr_count"] is None


def test_connect_adds_the_timeline_stamp_column_to_an_old_database(tmp_path):
    """The second column added after release, and the one a live box needs most.

    Without it the TTL that stops stage 3 re-reading the same 83 timelines every
    ten minutes has nowhere to record that it looked -- and the failure would be
    an OperationalError inside a cron run, after the sweep had already spent its
    quota, not a startup error anyone would see.
    """
    db = tmp_path / "old.sqlite"
    raw = sqlite3.connect(db)
    raw.executescript(OLD_SCHEMA)
    raw.commit()
    raw.close()

    conn = store.connect(db)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)")}
    assert {"open_pr_count", "pr_checked_at"} <= columns


def test_a_fresh_database_and_a_migrated_one_have_the_same_columns(tmp_path):
    """ADDED_COLUMNS' own docstring promises this, and nothing checked it.

    A column added to ADDED_COLUMNS but forgotten in SCHEMA gives a fresh box a
    table the migrated box does not have -- and the two hosts then disagree
    about what the tracker can remember, silently, until a query touches it.
    """
    old = tmp_path / "old.sqlite"
    raw = sqlite3.connect(old)
    raw.executescript(OLD_SCHEMA)
    raw.commit()
    raw.close()

    migrated = {row["name"] for row in
                store.connect(old).execute("PRAGMA table_info(candidates)")}
    fresh = {row["name"] for row in
             store.connect(tmp_path / "fresh.sqlite").execute(
                 "PRAGMA table_info(candidates)")}
    assert migrated == fresh
