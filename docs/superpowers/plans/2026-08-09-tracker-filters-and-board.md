# Tracker Filters and Bounty Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the tracker discarding winnable bounties on three wrong filters, and expose the ~529 candidates it currently holds back invisibly.

**Architecture:** Four independent changes to an existing SQLite-backed pipeline (`hunt.py`), each shipping on its own. B replaces a boolean "is there an open PR" reject with a claim test that only fires when an open PR's author is already assigned. C makes the assignee reject source-aware, because Expensify assigns an internal engineer at posting time. A replaces an Algora-label-gated farm reject with direct closed-bounty evidence plus a scoring penalty. D adds a new pure-rendering module that publishes the whole inventory to a private gist.

**Tech Stack:** Python 3, stdlib `sqlite3`, `requests`, `pytest`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-09-tracker-filters-and-board-design.md`

## Global Constraints

- **No test may touch the network.** `tests/conftest.py` replaces `requests.get` inside `sniper.github_api` with `FakeGet`; live payloads are recorded once by `tools/record_fixtures.py` and committed under `tests/fixtures/`.
- **"I could not look" and "there is nothing" must never be the same value.** Any helper that can fail returns `None` for unknown, and callers must abstain on `None` rather than treat it as zero. This is the failure the whole pipeline is built against.
- **A rejected candidate is stored with its reason, never deleted.** Deleting would make "filtered out" and "never discovered" identical.
- **A filter change must reach rows already in the table.** Prospective-only filtering hides its own effect; `store.apply_dismissals` and `store.apply_payout_rejections` are the existing precedent.
- Search quota is 30/min, core quota is 5000/hr. Search calls go through `store.reserve_or_wait(conn, "search", n)`; core calls through `store.reserve_or_wait(conn, "core", n)`.
- Run the suite with `python3 -m pytest -q` from the repo root. It is 212 tests today and must stay green.
- Work happens in the `.worktrees/tracker` worktree on branch `bounty-tracker-impl`, matching how the tracker was built.

---

## File Structure

| file | responsibility | change |
| ---- | -------------- | ------ |
| `sniper/github_api.py` | HTTP against GitHub, quota-aware | `has_open_linked_pr` → `linked_pr_state`; add `closed_issue_count` |
| `sniper/models.py` | `Candidate` and amount parsing | add `assignees`, `open_pr_count`, `repo_created_at`, `repo_open_issues` |
| `sniper/filters.py` | free / repo rejects | assignee reject becomes source-aware |
| `sniper/payers.py` | payout history | label-agnostic farm rule + closed-bounty lookup |
| `sniper/score.py` | metadata ranking | open-PR competition term + farm penalty |
| `sniper/store.py` | SQLite state | column migration, `open_pr_count`, `clear_rejections` |
| `sniper/board.py` | **new** — render the inventory as markdown, publish to a gist | create |
| `sniper/config.py` | settings | `ASSIGNEE_NOT_A_CLAIM`, `BOUNTY_GIST_ID`, board thresholds |
| `hunt.py` | the pipeline | wire all of the above |

---

### Task 1: Linked-PR state instead of a linked-PR boolean

`has_open_linked_pr` answers the wrong question. An open PR is not a claim: Expensify selects by proposal and bots auto-raise PRs on Help Wanted issues; Algora allows many attempts and only a merge wins. The only open PR that means "taken" is one whose author is already assigned to the issue.

**Files:**
- Modify: `sniper/github_api.py:421-434` (replace `has_open_linked_pr`)
- Modify: `sniper/models.py:133-219` (`Candidate`: add `assignees`, `open_pr_count`, `with_linked_prs`)
- Test: `tests/test_github_api_timeline.py` (rewrite), `tests/test_models.py` (append)

**Interfaces:**
- Consumes: `github_api.iter_timeline(repo, number)`, already present.
- Produces:
  - `github_api.LinkedPRs(open_count: int, claimant: str | None)` — frozen dataclass
  - `github_api.linked_pr_state(repo: str, number: int, assignees: frozenset[str]) -> LinkedPRs`
  - `Candidate.assignees: tuple[str, ...]` (default `()`)
  - `Candidate.open_pr_count: int | None` (default `None` = never checked)
  - `Candidate.with_linked_prs(count: int) -> Candidate`

- [ ] **Step 1: Write the failing tests for `linked_pr_state`**

Replace the whole body of `tests/test_github_api_timeline.py` (keep the module docstring's spirit, update it) with:

```python
"""linked_pr_state(): is this bounty actually claimed, or merely contested?

An open cross-referencing PR used to reject the candidate outright. That is the
wrong question on every platform in play. Expensify selects by PROPOSAL -- a C+
picks one and only then is that person assigned and writes the PR -- and plenty
of people now run agents that auto-raise PRs on Help Wanted issues. Algora
allows many attempts on one issue and only a MERGE wins, so a later entrant may
read the open PRs, find their flaws, and propose better. Measured 2026-08-09 on
the live database, 'open linked PR' had rejected 562 candidates, more than the
536 it kept.

So the only open PR that means "taken" is one whose author is already an
assignee of the issue. Everything else is competition, and competition is
scored, not rejected.

The timeline endpoint is chosen over search deliberately -- it draws on the
5000/hr core quota instead of the 30/min search quota (HANDOFF.md trap #3).

Cross-references arrive from plain issues in unrelated repos too -- bounty-index
bots such as BountyScout open tracking ISSUES that reference the bounty. Only
entries whose source carries a `pull_request` object count.

The recorded fixture is a live snapshot and cross-refs churn: the PRs on one
issue can all close within days. If test_real_recorded_timeline_counts_open_prs
starts failing, re-record against a fresh target (tools/record_fixtures.py holds
the target list) -- never relax the assertion to match whatever the fixture now
holds.
"""

from sniper import github_api
from tests.conftest import load_fixture


def _xref(is_pr: bool, state: str, author: str = "stranger"):
    issue = {"state": state, "user": {"login": author}}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/x"}
    return {"event": "cross-referenced", "source": {"issue": issue}}


def test_open_pr_by_a_stranger_is_counted_not_a_claim(fake_get):
    fake_get.queue(200, [{"event": "labeled"}, _xref(True, "open", "randobot")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset({"alice"}))
    assert state.open_count == 1
    assert state.claimant is None


def test_open_pr_by_an_assignee_is_a_claim(fake_get):
    fake_get.queue(200, [_xref(True, "open", "alice")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset({"alice"}))
    assert state.claimant == "alice"


def test_assignee_match_ignores_login_case(fake_get):
    fake_get.queue(200, [_xref(True, "open", "Alice")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset({"alice"}))
    assert state.claimant == "Alice"


def test_closed_pr_by_an_assignee_is_not_a_claim(fake_get):
    fake_get.queue(200, [_xref(True, "closed", "alice")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset({"alice"}))
    assert state.open_count == 0
    assert state.claimant is None


def test_open_issue_cross_reference_does_not_count(fake_get):
    fake_get.queue(200, [_xref(False, "open", "alice")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset({"alice"}))
    assert state.open_count == 0
    assert state.claimant is None


def test_several_open_prs_are_all_counted(fake_get):
    fake_get.queue(200, [_xref(True, "open", "a"), _xref(True, "open", "b"),
                         _xref(True, "open", "c")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset())
    assert state.open_count == 3


def test_empty_timeline_is_zero_not_an_error(fake_get):
    fake_get.queue(200, [])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset())
    assert state == github_api.LinkedPRs(open_count=0, claimant=None)


def test_uses_the_timeline_endpoint_on_the_given_repo(fake_get):
    fake_get.queue(200, [])
    github_api.linked_pr_state("acme/widgets", 42, frozenset())
    url, params = fake_get.calls[0]
    assert url == "https://api.github.com/repos/acme/widgets/issues/42/timeline"
    assert params["per_page"] == 100


def test_reads_the_whole_timeline_because_the_count_matters(fake_get):
    # The old boolean could stop at the first open PR. A count cannot: stopping
    # early would under-report competition and silently flatter the candidate.
    fake_get.queue(200, [_xref(True, "open", "a")] + [{"event": "commented"}] * 99)
    fake_get.queue(200, [_xref(True, "open", "b")])
    state = github_api.linked_pr_state("acme/widgets", 1, frozenset())
    assert state.open_count == 2
    assert [p["page"] for _, p in fake_get.calls] == [1, 2]


def test_iter_timeline_pages_while_pages_are_full(fake_get):
    fake_get.queue(200, [{"event": "commented"}] * 100)
    fake_get.queue(200, [{"event": "labeled"}])
    events = list(github_api.iter_timeline("acme/widgets", 1))
    assert len(events) == 101
    assert [p["page"] for _, p in fake_get.calls] == [1, 2]


def test_real_recorded_timeline_counts_open_prs(fake_get):
    fake_get.queue(200, load_fixture("timeline_with_open_pr"))
    state = github_api.linked_pr_state("arakoodev/EdgeChains", 279, frozenset())
    assert state.open_count > 0
    assert state.claimant is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_github_api_timeline.py -q`
Expected: FAIL — `AttributeError: module 'sniper.github_api' has no attribute 'linked_pr_state'`

- [ ] **Step 3: Replace `has_open_linked_pr` in `sniper/github_api.py`**

Delete lines 421-434 (`def has_open_linked_pr` through its `return False`) and put this in their place. Add `from dataclasses import dataclass` to the imports at the top of the file if it is not already there (`SearchResult` at line ~285 is a dataclass, so it will be).

```python
@dataclass(frozen=True)
class LinkedPRs:
    """What the open pull requests on an issue actually tell us.

    `open_count` is competition and belongs in the score. `claimant` is a claim
    and belongs in a reject: it is set only when an OPEN linked PR's author is
    already an assignee of the issue, which is the one pattern that means the
    work has been handed to someone. Selection happens by proposal on Expensify
    and by merge on Algora; in both cases the winner ends up assigned AND owning
    the PR, so this single test covers both platforms.
    """

    open_count: int
    claimant: str | None


def linked_pr_state(repo: str, number: int, assignees: frozenset[str]) -> LinkedPRs:
    """Count open linked PRs and spot one authored by an assignee.

    Cross-references from plain issues (including issues in unrelated repos,
    which do occur -- bounty-index bots open tracking issues) are ignored: only
    a `pull_request` source counts.

    Logins are compared case-insensitively; GitHub preserves the author's casing
    in payloads but treats logins case-insensitively, and an assignee list
    assembled from a different endpoint can disagree on case. `claimant` returns
    the author's login as GitHub spelled it, so the reject reason names the real
    account.

    This reads the WHOLE timeline. The predecessor short-circuited on the first
    open PR, which a count cannot do without under-reporting competition.
    """
    lowered = {login.lower() for login in assignees}
    open_count = 0
    claimant: str | None = None
    for event in iter_timeline(repo, number):
        if event.get("event") != "cross-referenced":
            continue
        source = (event.get("source") or {}).get("issue") or {}
        if not source.get("pull_request") or source.get("state") != "open":
            continue
        open_count += 1
        author = (source.get("user") or {}).get("login") or ""
        if claimant is None and author.lower() in lowered:
            claimant = author
    return LinkedPRs(open_count=open_count, claimant=claimant)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_github_api_timeline.py -q`
Expected: PASS, 12 tests.

- [ ] **Step 5: Write the failing test for the new `Candidate` fields**

Append to `tests/test_models.py`:

```python
def test_from_issue_records_assignee_logins():
    payload = {"number": 1, "title": "t", "labels": [],
               "assignees": [{"login": "alice"}, {"login": "Bob"}]}
    cand = Candidate.from_issue("github-labels", "acme/widgets", payload)
    assert cand.assignees == ("alice", "Bob")
    assert cand.has_assignee is True


def test_from_issue_records_the_legacy_single_assignee_field_too():
    # GitHub still emits `assignee` alongside `assignees`; a payload carrying
    # only the singular form must not produce an empty login list, or the
    # claim test in linked_pr_state silently never fires.
    payload = {"number": 1, "title": "t", "labels": [],
               "assignee": {"login": "carol"}}
    cand = Candidate.from_issue("github-labels", "acme/widgets", payload)
    assert cand.assignees == ("carol",)


def test_from_issue_deduplicates_assignees_preserving_order():
    payload = {"number": 1, "title": "t", "labels": [],
               "assignee": {"login": "carol"},
               "assignees": [{"login": "carol"}, {"login": "dave"}]}
    cand = Candidate.from_issue("github-labels", "acme/widgets", payload)
    assert cand.assignees == ("carol", "dave")


def test_unassigned_issue_has_no_logins_and_no_flag():
    payload = {"number": 1, "title": "t", "labels": []}
    cand = Candidate.from_issue("github-labels", "acme/widgets", payload)
    assert cand.assignees == ()
    assert cand.has_assignee is False


def test_open_pr_count_starts_unknown_and_is_set_explicitly():
    payload = {"number": 1, "title": "t", "labels": []}
    cand = Candidate.from_issue("github-labels", "acme/widgets", payload)
    assert cand.open_pr_count is None
    assert cand.with_linked_prs(2).open_pr_count == 2
    assert cand.with_linked_prs(0).open_pr_count == 0
```

Check the top of `tests/test_models.py` for how it imports `Candidate` and match it.

- [ ] **Step 6: Run to verify it fails**

Run: `python3 -m pytest tests/test_models.py -q -k "assignee or open_pr_count"`
Expected: FAIL — `AttributeError: 'Candidate' object has no attribute 'assignees'`

- [ ] **Step 7: Add the fields to `Candidate`**

In `sniper/models.py`, in the defaults block that currently starts at line 148, insert two fields at the top of that block so the comment about enrichment still applies to the ones below it:

```python
    # Assignee logins, alongside the has_assignee flag rather than replacing it.
    # The flag is what filters.free_reject reads; the logins are what
    # github_api.linked_pr_state needs to recognise a PR author who is already
    # assigned. Keeping both avoids rewriting every Candidate(...) in the suite.
    assignees: tuple[str, ...] = ()
    # None means the timeline was never read, never "there are no PRs". Stage 3
    # can be skipped when the core budget runs out, and a skipped check that
    # scored as zero competition would flatter the candidate.
    open_pr_count: int | None = None
    # Filled by with_repo_detail(); None means "not enriched", never "zero".
    language: str | None = None
```

Replace line 190 (`has_assignee = bool(...)`) with:

```python
        # Collect from both shapes. GitHub emits `assignees` and still emits the
        # legacy singular `assignee`; a payload carrying only the singular form
        # would otherwise yield an empty login list, and the claim test would
        # silently never fire.
        logins: list[str] = []
        for entry in ([payload.get("assignee")] + list(payload.get("assignees") or [])):
            login = (entry or {}).get("login")
            if login and login not in logins:
                logins.append(login)
        has_assignee = bool(logins)
```

Add `assignees=tuple(logins),` to the `cls(...)` call, next to `has_assignee=has_assignee,`.

Add the new method after `with_conservative_amount`:

```python
    def with_linked_prs(self, count: int) -> Candidate:
        """Record how many OPEN pull requests reference this issue."""
        return replace(self, open_pr_count=count)
```

- [ ] **Step 8: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS. Any failure here is a caller of `has_open_linked_pr` — `hunt.py:209` is expected to fail at import/attribute level only when exercised; if a test covers it, note the name and fix it in Task 3 rather than patching around it now. If `hunt.py` tests fail, stop and do Task 3 before committing.

- [ ] **Step 9: Commit**

```bash
git add sniper/github_api.py sniper/models.py tests/test_github_api_timeline.py tests/test_models.py
git commit -m "feat: an open PR is competition; only an assignee's own PR is a claim

has_open_linked_pr answered the wrong question and it was the tracker's most
expensive mistake: measured on the live database, it had rejected 562 candidates,
more than the 536 it kept.

An open PR is not a claim anywhere in this market. Expensify selects by proposal
-- a C+ picks one, and only then is that person assigned and writes the PR --
while plenty of people run agents that auto-raise PRs on Help Wanted issues.
Algora allows many attempts on one issue and only a merge wins, so a later
entrant is entitled to read the open PRs, find their flaws and do better.

linked_pr_state returns the open count and, separately, an author who is already
an assignee. That second case is the one pattern that means the work has been
handed to someone, and it is the same on both platforms, so stage 3 needs no
per-source branch."
```

---

### Task 2: Store the PR count, and re-judge the rows already rejected

**Files:**
- Modify: `sniper/store.py:21-94` (schema + `connect`), `176-233` (`upsert_candidate`)
- Modify: `sniper/store.py` (append `clear_rejections`)
- Test: `tests/test_store_candidates.py` (append), `tests/test_store_migration.py` (create)

**Interfaces:**
- Consumes: `Candidate.open_pr_count` from Task 1.
- Produces:
  - `candidates.open_pr_count INTEGER` column, written by `upsert_candidate`
  - `store.clear_rejections(conn, reason: str) -> int`

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_store_migration.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_store_migration.py -q`
Expected: FAIL — `assert 'open_pr_count' in columns`

- [ ] **Step 3: Add the column and the migration**

In `sniper/store.py`, add `open_pr_count  INTEGER,` to the `candidates` table in `SCHEMA`, immediately after the `score REAL,` line, so fresh databases are born with it.

Then add below `SCHEMA` (after line 85):

```python
# Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an
# existing database on whatever shape it was created with, so every later column
# has to be added explicitly. Keep this list append-only and keep the type
# declarations identical to SCHEMA's, so a migrated database and a fresh one are
# indistinguishable.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "candidates": {"open_pr_count": "INTEGER"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, wanted in ADDED_COLUMNS.items():
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, declaration in wanted.items():
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    conn.commit()
```

In `connect()`, add `_migrate(conn)` on the line after `conn.executescript(SCHEMA)`.

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_store_migration.py -q`
Expected: PASS, 3 tests.

- [ ] **Step 5: Write the failing tests for persistence and `clear_rejections`**

Append to `tests/test_store_candidates.py` (match its existing imports and its helper for building a `Candidate` — if it has one, reuse it rather than writing a new one):

```python
def test_open_pr_count_round_trips(tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    cand = _candidate(number=1).with_linked_prs(3)
    store.upsert_candidate(conn, cand, now=1.0)
    row = conn.execute("SELECT open_pr_count FROM candidates").fetchone()
    assert row["open_pr_count"] == 3


def test_unchecked_candidate_stores_null_not_zero(tmp_path):
    # "the timeline was never read" and "there are no PRs" must not collapse.
    conn = store.connect(tmp_path / "s.sqlite")
    store.upsert_candidate(conn, _candidate(number=1), now=1.0)
    row = conn.execute("SELECT open_pr_count FROM candidates").fetchone()
    assert row["open_pr_count"] is None


def test_open_pr_count_is_updated_on_a_later_sighting(tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    store.upsert_candidate(conn, _candidate(number=1).with_linked_prs(1), now=1.0)
    store.upsert_candidate(conn, _candidate(number=1).with_linked_prs(4), now=2.0)
    row = conn.execute("SELECT open_pr_count FROM candidates").fetchone()
    assert row["open_pr_count"] == 4


def test_clear_rejections_revives_only_the_named_reason(tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    store.upsert_candidate(conn, _candidate(number=1), rejected="open linked PR", now=1.0)
    store.upsert_candidate(conn, _candidate(number=2), rejected="archived repo", now=1.0)
    store.upsert_candidate(conn, _candidate(number=3), now=1.0)

    changed = store.clear_rejections(conn, "open linked PR")

    assert changed == 1
    live = {row["number"] for row in
            conn.execute("SELECT number FROM candidates WHERE rejected IS NULL")}
    assert live == {1, 3}


def test_clear_rejections_returns_zero_when_nothing_matches(tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    store.upsert_candidate(conn, _candidate(number=1), rejected="fork", now=1.0)
    assert store.clear_rejections(conn, "open linked PR") == 0
```

If `tests/test_store_candidates.py` has no `_candidate` helper, add this one at the top of the file:

```python
def _candidate(*, number: int, source: str = "github-labels",
               repo: str = "acme/widgets") -> Candidate:
    return Candidate(
        source=source, repo=repo, number=number, title="t",
        url=f"https://github.com/{repo}/issues/{number}", state="open",
        comments=0, labels=(), created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z", has_assignee=False,
        amount_usd=100, amounts_seen=(100,))
```

- [ ] **Step 6: Run to verify it fails**

Run: `python3 -m pytest tests/test_store_candidates.py -q -k "open_pr_count or clear_rejections"`
Expected: FAIL — `sqlite3.OperationalError: no such column: open_pr_count` on the first, `AttributeError: module 'sniper.store' has no attribute 'clear_rejections'` on the last two.

- [ ] **Step 7: Persist the column and add `clear_rejections`**

In `upsert_candidate`, add `open_pr_count` to the INSERT column list (after `score`), add one more `?` to the VALUES tuple, and add `cand.open_pr_count` to the parameters in the same position. Do the same for the UPDATE: add `open_pr_count = ?` after `score = ?` and `cand.open_pr_count` to its parameters in that position.

Append to `sniper/store.py`:

```python
def clear_rejections(conn: sqlite3.Connection, reason: str) -> int:
    """Un-reject stored candidates that were dropped for `reason`. Rows changed.

    A filter that changes its mind has to reach the rows it already judged. When
    the open-linked-PR rule was replaced, 562 stored candidates were sitting
    rejected under the old question -- and because a candidate is only re-judged
    when GitHub happens to touch it again, a months-old bounty could have stayed
    wrongly rejected forever. Clearing the reason puts them back in front of the
    new rule on the next sweep.

    Matches the reason EXACTLY. Reasons that interpolate a value (a repo's open
    count, a claimant's login) are deliberately not clearable this way: a prefix
    match would be a blunt instrument aimed at a table this is meant to correct.
    """
    cur = conn.execute(
        "UPDATE candidates SET rejected = NULL WHERE rejected = ?", (reason,))
    conn.commit()
    return cur.rowcount
```

- [ ] **Step 8: Run to verify it passes**

Run: `python3 -m pytest tests/test_store_candidates.py tests/test_store_migration.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add sniper/store.py tests/test_store_candidates.py tests/test_store_migration.py
git commit -m "feat: store the open-PR count, and let a changed filter reach the rows it already judged

Two things the schema could not do. CREATE TABLE IF NOT EXISTS is not a
migration -- every deployed database keeps the shape it was created with, and
the failure mode is an OperationalError deep inside a cron run that has already
spent its quota, so new columns are now added explicitly and idempotently.

And clear_rejections lets a rule that changed its mind revisit its verdicts. A
candidate is otherwise only re-judged when GitHub happens to touch it again,
which for a months-old bounty can be never: 562 rows were sitting rejected under
a question the tracker no longer asks.

open_pr_count is NULL when the timeline was never read, never 0. Stage 3 is
skippable when the core budget runs out, and a skipped check scored as zero
competition would flatter the candidate it never checked."
```

---

### Task 3: Wire the claim test into the pipeline and into the score

**Files:**
- Modify: `hunt.py:200-225` (stage 3), `hunt.py:145-160` (retroactive clear)
- Modify: `sniper/score.py:40-52` (`score`)
- Test: `tests/test_score.py` (append), `tests/test_hunt.py` (append, and rewrite `test_an_open_linked_pr_rejects_the_candidate` at line ~127)

**Interfaces:**
- Consumes: `github_api.linked_pr_state`, `Candidate.with_linked_prs`, `store.clear_rejections`.
- Produces: reject reason string `f"claimed by assignee @{login}"`; scoring term `1 + comments + 2 * open_pr_count`.

- [ ] **Step 1: Write the failing scoring test**

Append to `tests/test_score.py`:

`tests/test_score.py` already has `NOW`, `_at(days_ago)` and `_cand(*, amount_label, comments, stars, pushed_days)` at line 49. Reuse them exactly — do not add a second helper.

```python
def test_open_prs_count_as_competition():
    # An open PR is a rival who has already written code, so it costs more than
    # a comment. It is not a wall: on Expensify selection is by proposal, and on
    # Algora only a merge wins, so a better entrant can still take it.
    base = _cand(amount_label="$1000", comments=0)
    contested = base.with_linked_prs(2)
    assert scoring.score(contested, now=NOW) < scoring.score(base, now=NOW)
    # 1000 / (1 + 0 + 2*2) = 200, against 1000 / 1 = 1000, both at health 1.0
    assert scoring.score(contested, now=NOW) == pytest.approx(200.0)


def test_comments_and_open_prs_both_count():
    cand = _cand(amount_label="$1000", comments=3).with_linked_prs(1)
    # 1000 / (1 + 3 + 2) = 166.67
    assert scoring.score(cand, now=NOW) == pytest.approx(1000 / 6)


def test_an_unchecked_candidate_scores_as_if_uncontested():
    # open_pr_count is None when stage 3 never ran -- the core budget can run
    # out mid-run. Guessing a penalty would be inventing evidence about a check
    # we could not afford to make.
    base = _cand(amount_label="$1000", comments=0)
    assert base.open_pr_count is None
    assert scoring.score(base, now=NOW) == pytest.approx(1000.0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_score.py -q -k "competition or unchecked"`
Expected: FAIL — the contested score equals 300.0 because the term is not there yet.

- [ ] **Step 3: Add the competition term**

In `sniper/score.py`, replace the final line of `score()`:

```python
    health = repo_health(cand.stars, cand.pushed_at, now=now)
    return cand.amount_usd / (1 + cand.comments) * health
```

with:

```python
    health = repo_health(cand.stars, cand.pushed_at, now=now)
    # An open PR is a rival who has already written code, so it weighs more than
    # a comment -- but it is not a wall. Expensify selects by proposal and Algora
    # by merge, so a later entrant can read the open PRs and beat them. None
    # means stage 3 never ran, and inventing a penalty for a check we could not
    # afford would be inventing evidence.
    competition = 1 + cand.comments + 2 * (cand.open_pr_count or 0)
    return cand.amount_usd / competition * health
```

Update the module docstring's formula line from `score = amount_usd / (1 + comments) x repo_health` to `score = amount_usd / (1 + comments + 2 x open_prs) x repo_health`.

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_score.py -q`
Expected: PASS.

- [ ] **Step 5: Rewrite the stage-3 pipeline tests**

`tests/test_hunt.py` has `test_an_open_linked_pr_rejects_the_candidate` at line ~127, which asserts exactly the behaviour being removed. **Delete it** and put these in its place. They use the module's existing `_conn`, `_run`, `_cand`, `FakeSource` and `HEALTHY` — do not add new helpers.

```python
def _open_pr_by(login: str) -> dict:
    return {"event": "cross-referenced",
            "source": {"issue": {"pull_request": {"url": "u"}, "state": "open",
                                 "user": {"login": login}}}}


def test_an_open_pr_from_a_stranger_keeps_the_candidate(tmp_path, fake_get):
    # This is the change. Measured 2026-08-09, this rule had rejected 562
    # candidates -- more than the 536 it kept -- and most of those PRs were
    # opened by agents that auto-raise on any bounty label.
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [_open_pr_by("randobot")])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], dry_run=True)
    row = conn.execute("SELECT rejected, open_pr_count FROM candidates").fetchone()
    assert row["rejected"] is None
    assert row["open_pr_count"] == 1


def test_open_prs_are_counted_not_just_detected(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [_open_pr_by("a"), _open_pr_by("b"), _open_pr_by("c")])
    _run(conn, sources=[FakeSource("s", [_cand(1)])], dry_run=True)
    row = conn.execute("SELECT open_pr_count FROM candidates").fetchone()
    assert row["open_pr_count"] == 3


def test_a_contested_candidate_scores_below_an_uncontested_one(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [_open_pr_by("randobot")])
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [_cand(1), _cand(2)])], dry_run=True)
    scores = {r["number"]: r["score"]
              for r in conn.execute("SELECT number, score FROM candidates")}
    assert scores[1] < scores[2]


def test_stale_open_linked_pr_rejections_are_cleared_at_the_start_of_a_run(
        tmp_path, fake_get):
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand(99), rejected="open linked PR", now=NOW)
    _run(conn, sources=[FakeSource("s", [])], dry_run=True)
    row = conn.execute(
        "SELECT rejected FROM candidates WHERE number = 99").fetchone()
    assert row["rejected"] is None


def test_clearing_stale_rejections_is_announced_rather_than_silent(
        tmp_path, fake_get):
    # A silent revival looks identical to newly discovered inventory, which
    # would make a filter change unattributable the next time one is made.
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand(99), rejected="open linked PR", now=NOW)
    text = _run(conn, sources=[FakeSource("s", [])], dry_run=True)
    assert "re-judged" in text
```

- [ ] **Step 6: Run to verify it fails**

Run: `python3 -m pytest tests/test_hunt.py -q -k "stranger or counted or contested or stale or announced"`
Expected: FAIL — `AttributeError: module 'sniper.github_api' has no attribute 'has_open_linked_pr'` is already gone from Task 1, so `hunt.py` raises at the call site.

- [ ] **Step 7: Rewrite stage 3 in `hunt.py`**

Replace `hunt.py:208-215`:

```python
        try:
            taken = github_api.has_open_linked_pr(cand.repo, cand.number)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"timeline for {cand.repo}#{cand.number} failed: {exc}")
            continue
        if taken:
            store.upsert_candidate(conn, cand, rejected="open linked PR", now=stamp)
            continue
```

with:

```python
        try:
            linked = github_api.linked_pr_state(
                cand.repo, cand.number, frozenset(cand.assignees))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"timeline for {cand.repo}#{cand.number} failed: {exc}")
            continue
        # An open PR by someone already assigned is the one shape that means the
        # work has been handed out -- true on Expensify, where a C+ picks a
        # proposal and then assigns its author, and on Algora, where the winner
        # is whoever merges. Every other open PR is a rival, and rivals are
        # scored rather than rejected.
        if linked.claimant:
            store.upsert_candidate(
                conn, cand, rejected=f"claimed by assignee @{linked.claimant}",
                now=stamp)
            continue
        cand = cand.with_linked_prs(linked.open_count)
```

- [ ] **Step 8: Clear the stale rejections at the start of a run**

In `hunt.py`, immediately after `store.apply_dismissals(conn)` (line 150), add:

```python
    # The open-linked-PR rule was replaced by a claim test. Rows it rejected under
    # the old question have to be put back in front of the new one, or they stay
    # rejected until GitHub happens to touch them again -- which for a months-old
    # bounty is never. Costs one UPDATE and is a no-op once the table is drained.
    revived = store.clear_rejections(conn, "open linked PR")
    if revived:
        warnings.append(f"{revived} candidates re-judged: an open PR is no longer "
                        f"treated as a claim unless its author is assigned")
```

- [ ] **Step 9: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS, all tests.

- [ ] **Step 10: Commit**

```bash
git add hunt.py sniper/score.py tests/
git commit -m "fix: stop discarding bounties because a bot opened a PR on them

Stage 3 now rejects only when an open linked PR's author is already an assignee,
and scores every other open PR as competition: amount / (1 + comments + 2*prs).
An open PR weighs more than a comment because its author has already written
code, and less than a wall because on Expensify selection is by proposal and on
Algora only a merge wins -- a later entrant may read those PRs and do better.

The 562 candidates already rejected under the old question are cleared at the
start of the run so the new rule actually sees them, and the digest says how many
were re-judged rather than letting a silent revival look like new inventory."
```

---

### Task 4: Assignment is not a claim on Expensify

Expensify assigns an internal engineer the moment a job is posted. Verified 2026-08-09: issue #97949 is open and accepting proposals with labels `External, Daily, Help Wanted` and assignee `stitesExpensify`. So `free_reject`'s blanket assignee rule rejects 100% of Expensify candidates at stage 1, before Task 3's rule is ever reached.

**Files:**
- Modify: `sniper/filters.py:20-55` (`free_reject`)
- Modify: `sniper/config.py` (add `ASSIGNEE_NOT_A_CLAIM`)
- Test: `tests/test_filters.py` (append), `tests/test_hunt.py` (append the end-to-end claim case)

**Interfaces:**
- Consumes: `Candidate.source`, `Candidate.has_assignee`.
- Produces: `config.ASSIGNEE_NOT_A_CLAIM: frozenset[str]`.

- [ ] **Step 1: Write the failing test**

`tests/test_filters.py:20` has `_cand(**over)`, which hardcodes the source in `Candidate.from_search_item("github-labels", base)`. Give it a source parameter first:

```python
def _cand(source="github-labels", **over):
    base = {
        "number": 7, "title": "t", "state": "open", "comments": 1,
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-08-01T00:00:00Z",
        "html_url": "u", "repository_url": "https://api.github.com/repos/acme/widgets",
        "assignee": None, "labels": [{"name": "$780"}],
    }
    base.update(over)
    return Candidate.from_search_item(source, base)
```

Then append:

```python
def test_an_assigned_expensify_issue_is_not_rejected():
    # Verified 2026-08-09 against the live API: #97949 is open and accepting
    # proposals, labelled External/Daily/Help Wanted, assigned to
    # stitesExpensify. Expensify assigns an internal engineer when the job is
    # posted and a C+ when one volunteers; neither is a contributor taking the
    # work. Rejecting on it darkened the entire $250 channel, silently, because
    # a rejected candidate looks exactly like one that was never posted.
    cand = _cand("expensify", assignee={"login": "stitesExpensify"})
    assert filters.free_reject(cand) is None


def test_an_assigned_issue_on_every_other_source_is_still_rejected():
    assert filters.free_reject(
        _cand("github-labels", assignee={"login": "x"})) == "assigned"


def test_the_expensify_exemption_is_only_about_assignment():
    # It says nothing about closed, rewarded or cheap.
    assert filters.free_reject(
        _cand("expensify", assignee={"login": "x"}, state="closed")) == "closed"
    assert filters.free_reject(
        _cand("expensify", assignee={"login": "x"},
              labels=[{"name": "$780"}, {"name": "💰 Rewarded"}])) == "already rewarded"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_filters.py -q -k expensify`
Expected: FAIL — `assert 'assigned' is None`

- [ ] **Step 3: Add the config knob**

In `sniper/config.py`, near the other source settings (beside `HUNT_ENABLED_SOURCES`), add:

```python
# Sources where an assignee tells us nothing about whether the work is taken.
#
# Expensify assigns an internal engineer the moment a job is posted, and a
# Contributor+ when one volunteers. Verified 2026-08-09: #97949 was open and
# accepting proposals, labelled External/Daily/Help Wanted, assigned to
# stitesExpensify. The blanket assignee reject therefore dropped 100% of
# Expensify candidates at stage 1 -- the whole $250 channel, silently.
#
# Whether such a job has actually been awarded is answered in stage 3 instead:
# once a C+ picks a proposal, its author is assigned AND opens the PR, which is
# exactly what github_api.linked_pr_state calls a claim.
ASSIGNEE_NOT_A_CLAIM = frozenset(
    name.strip()
    for name in os.environ.get("ASSIGNEE_NOT_A_CLAIM", "expensify").split(",")
    if name.strip()
)
```

- [ ] **Step 4: Make the reject source-aware**

In `sniper/filters.py`, replace lines 37-38:

```python
    if cand.has_assignee:
        return "assigned"
```

with:

```python
    # An assignee is a claim on most sources and pure noise on Expensify, which
    # assigns an internal engineer at posting time. See config.ASSIGNEE_NOT_A_CLAIM.
    # Those sources are judged by stage 3's claim test instead, which is the
    # sharper question everywhere: is an assignee also the author of an open PR?
    if cand.has_assignee and cand.source not in config.ASSIGNEE_NOT_A_CLAIM:
        return "assigned"
```

Extend the `free_reject` docstring with one line: `Sources listed in config.ASSIGNEE_NOT_A_CLAIM are exempt from the assignee rule.`

- [ ] **Step 5: Run to verify it passes**

Run: `python3 -m pytest tests/test_filters.py -q`
Expected: PASS.

- [ ] **Step 6: Add the end-to-end claim case**

This is Task 1's `claimant` path proven through the whole pipeline. It could not be written in Task 3, because until this task an assigned candidate was rejected at stage 1 and never reached stage 3 at all.

First give `tests/test_hunt.py`'s `_cand` a source parameter, matching what Task 4 Step 1 did to `test_filters.py`:

```python
def _cand(number, *, labels=None, assignee=None, state="open", comments=0,
          repo="acme/widgets", source="github-labels"):
    return Candidate.from_search_item(source, {
        "number": number, "title": f"issue {number}", "state": state,
        "comments": comments, "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "assignee": assignee, "labels": labels or [{"name": "$780"}],
    })
```

Then append:

```python
def test_an_assigned_expensify_issue_reaches_stage_three(tmp_path, fake_get):
    # The exemption is not a free pass -- it moves the question from "is anyone
    # assigned" to "is an assignee also writing the PR", which is the sharper
    # test. This candidate is assigned and survives, because nobody has a PR.
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [])
    _run(conn, sources=[FakeSource("s", [
        _cand(1, source="expensify", assignee={"login": "stitesExpensify"})])],
        dry_run=True)
    assert conn.execute("SELECT rejected FROM candidates").fetchone()["rejected"] is None


def test_an_assignee_who_opened_the_pr_is_a_claim(tmp_path, fake_get):
    # Expensify's actual award: a C+ picks a proposal, its author is assigned,
    # and that author opens the PR. That pair is the claim -- and it is the same
    # pair on Algora, where the winner is whoever merges.
    conn = _conn(tmp_path)
    fake_get.queue(200, HEALTHY, {"ETag": 'W/"r"'})
    fake_get.queue(200, [_open_pr_by("daledah")])
    _run(conn, sources=[FakeSource("s", [
        _cand(1, source="expensify", assignee={"login": "daledah"})])],
        dry_run=True)
    row = conn.execute("SELECT rejected FROM candidates").fetchone()
    assert row["rejected"] == "claimed by assignee @daledah"
```

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add sniper/filters.py sniper/config.py tests/test_filters.py
git commit -m "fix: an Expensify assignee is the reviewer, not the winner

Expensify assigns an internal engineer the moment a job is posted and a C+ when
one volunteers. Verified against the live API on 2026-08-09: #97949 is open and
accepting proposals, labelled External/Daily/Help Wanted, assigned to
stitesExpensify.

So free_reject's blanket assignee rule was rejecting 100% of Expensify
candidates at stage 1 -- the entire \$250 channel, and silently, because a
rejected candidate looks exactly like one that was never posted. It had not
surfaced yet only because the watch cron is hours old and Help Wanted lands about
twice a day.

Whether such a job has actually been awarded is now answered by stage 3: a C+
picks a proposal, its author is assigned and opens the PR, and that is precisely
the claim linked_pr_state tests for."
```

---

### Task 5: Reject farms on closed-bounty evidence, not on Algora's label

`zhangjiayang6835-cyber/bounty-plaza` holds three of the top four digest slots. Measured 2026-08-09: created 2026-07-08, 5 stars, 526 open issues, `💎 Bounty` count 0, `💰 Rewarded` count 0, advertising "Add a shrimp-person species 🦐" at $200. `payout_reject` cannot see it because that rule only fires on Algora's own label and this farm rolls its own.

**Files:**
- Modify: `sniper/payers.py` (add `bounty_label_of`, `ever_closed_with`; rewrite `payout_reject`)
- Modify: `sniper/store.py:462-499` (`apply_payout_rejections` takes a callback)
- Modify: `hunt.py:152-188`
- Test: `tests/test_payers.py` (append — find the real filename with `ls tests/ | grep -i payer`), `tests/test_store_budget.py` or wherever `apply_payout_rejections` is tested

**Interfaces:**
- Consumes: `store.get_cache`, `store.set_cache`, `store.reserve_or_wait`, `github_api.search_issues`.
- Produces:
  - `payers.bounty_label_of(labels: tuple[str, ...]) -> str | None`
  - `payers.ever_closed_with(conn, repo: str, label: str, *, now: float | None = None) -> int | None`
  - `payers.payout_reject(repo, labels, open_bounties_here, payers, closed_count: int | None) -> str | None`
  - `store.apply_payout_rejections(conn, payers, threshold, closed_for)` where `closed_for(repo: str, label: str) -> int | None`

- [ ] **Step 1: Write the failing tests for `bounty_label_of`**

Append to the payers test module:

```python
def test_prefers_algoras_own_label():
    assert payers.bounty_label_of(("bug", "💎 Bounty", "$500")) == "💎 Bounty"


def test_falls_back_to_any_label_containing_bounty():
    # bounty-plaza rolls its own plain "Bounty" label, which is exactly why the
    # Algora-only rule could not see 526 open bounties and 0 ever closed.
    assert payers.bounty_label_of(("Bounty", "python")) == "Bounty"


def test_bounty_match_is_case_insensitive():
    assert payers.bounty_label_of(("BOUNTY",)) == "BOUNTY"


def test_falls_back_to_an_amount_label():
    assert payers.bounty_label_of(("help wanted", "$250")) == "$250"


def test_returns_none_when_nothing_marks_this_as_a_bounty():
    # A repo's silence about a label it does not use says nothing, so the rule
    # must abstain rather than guess.
    assert payers.bounty_label_of(("bug", "good first issue")) is None
    assert payers.bounty_label_of(()) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/ -q -k bounty_label_of`
Expected: FAIL — `AttributeError: module 'sniper.payers' has no attribute 'bounty_label_of'`

- [ ] **Step 3: Implement `bounty_label_of`**

Append to `sniper/payers.py`:

```python
def bounty_label_of(labels: tuple[str, ...]) -> str | None:
    """The label to test this repo's payout history against, or None to abstain.

    Preference order is evidence order. Algora's own label is unambiguous.
    Anything containing "bounty" is a self-declared bounty programme, which is
    what bounty-plaza wears -- 526 open, 0 ever closed, invisible to a rule that
    demanded Algora's exact string. An amount label is the weakest of the three
    and is only reached when the other two are absent.

    None means the candidate carries nothing marking it as a bounty at all, and
    the caller must abstain: a repo's silence about a label it does not use says
    nothing about whether it pays.
    """
    for label in labels:
        if label.strip() == ALGORA_LABEL:
            return label
    for label in labels:
        if "bounty" in label.lower():
            return label
    for label in labels:
        if parse_amount_label(label) is not None:
            return label
    return None
```

Add `parse_amount_label` to the existing `from .models import repo_from_url` line so it reads `from .models import parse_amount_label, repo_from_url`.

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/ -q -k bounty_label_of`
Expected: PASS, 5 tests.

- [ ] **Step 5: Write the failing tests for `ever_closed_with`**

```python
def test_counts_closed_issues_carrying_the_label(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, {"total_count": 173, "incomplete_results": False, "items": []})
    assert payers.ever_closed_with(conn, "acme/widgets", "💎 Bounty", now=100.0) == 173


def test_a_farm_reports_zero(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, {"total_count": 0, "incomplete_results": False, "items": []})
    assert payers.ever_closed_with(conn, "farm/repo", "Bounty", now=100.0) == 0


def test_a_failure_is_none_not_zero(fake_get, tmp_path):
    # "I could not look" must never render as "there are none": that is the
    # single distinction this whole pipeline is built against, and here it is the
    # difference between abstaining and rejecting a real payer's whole backlog.
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(403, {"message": "rate limited"})
    assert payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=100.0) is None


def test_the_answer_is_cached_and_the_second_call_makes_no_request(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, {"total_count": 4, "incomplete_results": False, "items": []})
    assert payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=100.0) == 4
    assert payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=200.0) == 4
    assert len(fake_get.calls) == 1


def test_the_cache_expires(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, {"total_count": 4, "incomplete_results": False, "items": []})
    fake_get.queue(200, {"total_count": 9, "incomplete_results": False, "items": []})
    payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=100.0)
    later = 100.0 + config.PAYER_CACHE_S + 1
    assert payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=later) == 9


def test_the_query_asks_for_closed_issues_in_that_repo_with_that_label(
        fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, {"total_count": 0, "incomplete_results": False, "items": []})
    payers.ever_closed_with(conn, "acme/widgets", "💎 Bounty", now=100.0)
    _, params = fake_get.calls[0]
    assert params["q"] == 'repo:acme/widgets is:issue is:closed label:"💎 Bounty"'
    assert params["per_page"] == 1
```

Import `config` and `store` at the top of that test module if they are not already imported.

- [ ] **Step 6: Run to verify it fails**

Run: `python3 -m pytest tests/ -q -k ever_closed`
Expected: FAIL — `AttributeError: module 'sniper.payers' has no attribute 'ever_closed_with'`

- [ ] **Step 7: Implement `ever_closed_with`**

Append to `sniper/payers.py`:

```python
CLOSED_CACHE_PREFIX = "closed-bounty"


def ever_closed_with(
    conn: sqlite3.Connection,
    repo: str,
    label: str,
    *,
    now: float | None = None,
) -> int | None:
    """How many issues `repo` has ever CLOSED carrying `label`. None = unknown.

    This is the evidence that replaces "is it wearing Algora's label". A repo
    advertising a pile of open bounties and never closing one is a farm whatever
    label it uses: measured 2026-08-09, zhangjiayang6835-cyber/bounty-plaza had
    526 open, 0 ever closed, 5 stars, and a month of history, while holding three
    of the top four digest slots.

    Closed rather than rewarded, deliberately. `💰 Rewarded` is Algora's marker
    and a repo running its own programme never sets it -- commaai/opendbc has
    closed seven bounties including a $4,000 port with no Algora label anywhere.
    Closing a bounty is the weakest claim that still separates a working
    programme from a wall of adverts.

    Costs one search unit, cached for config.PAYER_CACHE_S. Returns None on any
    failure, and the caller MUST abstain rather than read that as zero.
    """
    stamp = time.time() if now is None else now
    key = f"{CLOSED_CACHE_PREFIX}:{repo}:{label}"
    _, body = store.get_cache(conn, key)
    if body:
        try:
            payload = json.loads(body)
            if stamp - payload["fetched_at"] < config.PAYER_CACHE_S:
                return payload["closed"]
        except (ValueError, KeyError, TypeError):
            pass  # unreadable cache is just a miss

    if not store.reserve_or_wait(conn, "search", 1):
        return None
    try:
        found = github_api.search_issues(
            f'repo:{repo} is:issue is:closed label:"{label}"',
            per_page=1, max_pages=1)
    except Exception:  # noqa: BLE001 -- unknown, and the caller abstains on it
        return None

    store.set_cache(conn, key, None,
                    json.dumps({"fetched_at": stamp, "closed": found.total_count}))
    return found.total_count
```

- [ ] **Step 8: Run to verify it passes**

Run: `python3 -m pytest tests/ -q -k ever_closed`
Expected: PASS, 6 tests.

`test_a_failure_is_none_not_zero` works because of how `_fetch_search_page` (github_api.py:248) is written: it re-raises rather than returning None when nothing has been collected yet, and page 1 always has nothing. So a 403 on the only page propagates as `RateLimited`, the `except Exception` catches it, and the caller gets `None`. That is the whole point — a throttled search and an empty repo must not produce the same answer.

- [ ] **Step 9: Write the failing tests for the rewritten `payout_reject`**

```python
def test_rejects_a_farm_using_its_own_label():
    reason = payers.payout_reject("farm/repo", ("Bounty",), 526, {"real/payer"},
                                  closed_count=0)
    assert reason == "no payout history (526 open bounties here, 0 ever closed)"


def test_keeps_a_repo_that_has_closed_even_one():
    assert payers.payout_reject("new/project", ("Bounty",), 30, {"real/payer"},
                                closed_count=1) is None


def test_abstains_when_the_closed_count_is_unknown():
    assert payers.payout_reject("farm/repo", ("Bounty",), 526, {"real/payer"},
                                closed_count=None) is None


def test_abstains_on_a_known_payer_without_asking():
    assert payers.payout_reject("real/payer", ("Bounty",), 999, {"real/payer"},
                                closed_count=0) is None


def test_abstains_below_the_open_count_threshold():
    # One or two unclosed bounties is a new project, not a farm.
    assert payers.payout_reject("small/repo", ("Bounty",), 2, {"real/payer"},
                                closed_count=0) is None


def test_abstains_when_no_label_marks_it_as_a_bounty():
    assert payers.payout_reject("some/repo", ("bug",), 526, {"real/payer"},
                                closed_count=0) is None


def test_abstains_when_the_payer_roster_is_unavailable():
    # An empty roster means the lookup failed, not that nobody pays.
    assert payers.payout_reject("farm/repo", ("Bounty",), 526, set(),
                                closed_count=0) is None
```

- [ ] **Step 10: Run to verify it fails**

Run: `python3 -m pytest tests/ -q -k payout_reject`
Expected: FAIL — `TypeError: payout_reject() got an unexpected keyword argument 'closed_count'`, and existing `payout_reject` tests that pass Algora-label candidates will also fail. Rewrite those existing tests to pass `closed_count=` explicitly; the ones asserting "not an Algora repo so keep" must become "no bounty-ish label at all so keep", since that is now the abstain condition.

- [ ] **Step 11: Rewrite `payout_reject`**

Replace `sniper/payers.py:101-129` entirely:

```python
def payout_reject(
    repo: str,
    labels: tuple[str, ...],
    open_bounties_here: int,
    payers: set[str],
    closed_count: int | None,
) -> str | None:
    """Reason to drop a candidate on payout history. None = keep.

    Still deliberately narrow. It fires only when all of these hold:

      * the payer roster was actually fetched (an empty one means the lookup
        failed, not that nobody pays);
      * the repo has never appeared in that roster;
      * the candidate carries some label marking it as a bounty, so the repo's
        silence is about something it actually claims to run;
      * it is advertising at least `config.UNPAID_REPO_THRESHOLD` open bounties,
        which is a farm's shape rather than a new project's;
      * and it has never once CLOSED a bounty carrying that label.

    The last condition replaces the old requirement that the repo wear Algora's
    exact `💎 Bounty` string. That gate existed for a real reason -- a repo with
    no Algora label has no payout record to be absent -- but it meant any farm
    using its own label was invisible. Measured 2026-08-09,
    zhangjiayang6835-cyber/bounty-plaza held three of the top four digest slots
    with 526 open bounties, 0 ever closed, 5 stars and a month of history, all
    behind a plain "Bounty" label.

    `closed_count` of None means the question could not be answered, and this
    abstains. A repo running its own programme, or a small repo with one or two
    genuine bounties, is never touched.
    """
    if not payers:
        return None  # roster unavailable: abstain rather than reject the world
    if repo in payers:
        return None
    if bounty_label_of(labels) is None:
        return None  # nothing marks this as a bounty; its silence says nothing
    if open_bounties_here < config.UNPAID_REPO_THRESHOLD:
        return None
    if closed_count is None or closed_count > 0:
        return None
    return (f"no payout history ({open_bounties_here} open bounties here, "
            f"0 ever closed)")
```

- [ ] **Step 12: Run to verify it passes**

Run: `python3 -m pytest tests/ -q -k "payout_reject or bounty_label_of or ever_closed"`
Expected: PASS.

- [ ] **Step 13: Convert `apply_payout_rejections` to a callback**

In `sniper/store.py`, change the signature and body of `apply_payout_rejections`:

```python
def apply_payout_rejections(conn: sqlite3.Connection, payers: set[str],
                            threshold: int, closed_for) -> int:
    """Re-judge STORED candidates against payout history. Returns rows changed.

    Filters that arrive later must reach the rows already in the table, for the
    same reason dismissals do. When the payout filter shipped, every candidate
    discovered before it stayed live -- so the digest kept recommending a repo
    with 201 open bounties and zero payouts until GitHub happened to touch each
    issue again.

    `closed_for(repo, label) -> int | None` supplies the closed-bounty count and
    is called LAZILY, only for repos that are over the threshold and off the
    payer roster. That matters: it costs a search unit each, and asking about
    every repo in the table would blow the 30/min pool on rows that were never
    going to be rejected. None means unknown, and this abstains on it.

    Uses only stored columns (`labels` is kept as JSON precisely so a judgement
    can be revisited), so the callback is the only cost.
    """
    if not payers:
        return 0  # roster unavailable: abstain, exactly as payout_reject does

    counts = {row["repo"]: row["n"] for row in conn.execute(
        "SELECT repo, COUNT(*) AS n FROM candidates WHERE rejected IS NULL GROUP BY repo")}
    closed_cache: dict[tuple[str, str], int | None] = {}
    changed = 0
    rows = conn.execute(
        "SELECT repo, number, labels FROM candidates WHERE rejected IS NULL").fetchall()
    for row in rows:
        repo = row["repo"]
        if repo in payers or counts.get(repo, 0) < threshold:
            continue
        try:
            labels = tuple(str(name) for name in json.loads(row["labels"]))
        except (ValueError, TypeError):
            continue
        label = payers_module.bounty_label_of(labels)
        if label is None:
            continue
        if (repo, label) not in closed_cache:
            closed_cache[(repo, label)] = closed_for(repo, label)
        reason = payers_module.payout_reject(
            repo, labels, counts[repo], payers, closed_cache[(repo, label)])
        if reason is None:
            continue
        conn.execute(
            "UPDATE candidates SET rejected = ? WHERE repo = ? AND number = ?",
            (reason, repo, row["number"]))
        changed += 1
    conn.commit()
    return changed
```

`store.py` cannot `from . import payers` at module scope — `payers` imports `store`. Import it inside the function instead, as the first line of the body:

```python
    from . import payers as payers_module
```

Add a one-line comment above it: `# Imported here, not at module scope: payers imports store.`

- [ ] **Step 14: Update the `apply_payout_rejections` callers and tests**

In `hunt.py`, replace lines 155-158:

```python
    payer_repos, payer_warnings = payers.known_payers(conn, now=stamp)
    warnings.extend(payer_warnings)
    store.apply_payout_rejections(conn, payer_repos, config.UNPAID_REPO_THRESHOLD,
                                  payers.ALGORA_LABEL)
```

with:

```python
    payer_repos, payer_warnings = payers.known_payers(conn, now=stamp)
    warnings.extend(payer_warnings)

    def closed_for(repo: str, label: str) -> int | None:
        return payers.ever_closed_with(conn, repo, label, now=stamp)

    store.apply_payout_rejections(conn, payer_repos, config.UNPAID_REPO_THRESHOLD,
                                  closed_for)
```

And in the stage-1 payout pass (lines 181-188), supply the count:

```python
    kept: list[Candidate] = []
    for cand in survivors:
        label = payers.bounty_label_of(cand.labels)
        closed = (closed_for(cand.repo, label)
                  if label and cand.repo not in payer_repos
                  and open_here[cand.repo] >= config.UNPAID_REPO_THRESHOLD
                  else None)
        reason = payers.payout_reject(
            cand.repo, cand.labels, open_here[cand.repo], payer_repos, closed)
        if reason:
            store.upsert_candidate(conn, cand, rejected=reason, now=stamp)
        else:
            kept.append(cand)
    survivors = kept
```

The guard before calling `closed_for` matters: without it every candidate costs a search unit, and `payout_reject` would abstain on nearly all of them anyway. `ever_closed_with` caches per `(repo, label)`, so repeated candidates in one repo cost one call.

Update every existing test that calls `apply_payout_rejections` to pass a callback, e.g. `lambda repo, label: 0` for the farm cases and `lambda repo, label: 5` for the keep cases.

- [ ] **Step 15: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS.

- [ ] **Step 16: Commit**

```bash
git add sniper/payers.py sniper/store.py hunt.py tests/
git commit -m "fix: a farm is a repo that never closes a bounty, whatever label it uses

The payout rule only fired on Algora's exact label, so a farm running its own
label was invisible to it. Measured 2026-08-09, zhangjiayang6835-cyber/bounty-plaza
held three of the top four digest slots: 526 open bounties, 0 ever closed, 5
stars, created a month earlier, advertising 'Add a shrimp-person species' at
\$200 behind a plain 'Bounty' label.

The label gate is replaced by direct evidence -- has this repo ever CLOSED a
bounty carrying the label it is advertising under. Closed rather than rewarded,
because '💰 Rewarded' is Algora's marker and a repo running its own programme
never sets it: commaai/opendbc has closed seven bounties including a \$4,000 port
with no Algora label anywhere.

The lookup costs one search unit per repo per day and is asked only about repos
already over the open-bounty threshold and off the payer roster. It returns None
when it could not look, and every caller abstains on None rather than reading it
as zero."
```

---

### Task 6: Farm-shaped repos sink instead of vanishing

`payout_reject` abstains on a repo that has closed even one bounty. A farm that closes the occasional issue therefore survives it. Metadata already in hand can push such a repo down without removing it, so the operator can still see and judge it.

**Files:**
- Modify: `sniper/models.py:211-219` (`with_repo_detail`), `133-155` (two more fields)
- Modify: `sniper/score.py` (add `farm_penalty`, use it in `score`)
- Test: `tests/test_score.py` (append), `tests/test_models.py` (append)

**Interfaces:**
- Consumes: the repo-detail payload already fetched by `hunt.enrich`.
- Produces:
  - `Candidate.repo_created_at: str | None`, `Candidate.repo_open_issues: int | None`
  - `score.farm_penalty(cand, *, now) -> float`

- [ ] **Step 1: Write the failing enrichment test**

Append to `tests/test_models.py`:

```python
def test_repo_detail_captures_the_repos_own_age_and_backlog():
    # Both arrive in the SAME payload the enrichment already fetches, so this
    # costs nothing. repo_created_at is the REPOSITORY's birthday -- created_at
    # on the Candidate is the issue's, and conflating them would rate a
    # month-old farm as a decade-old project.
    payload = {"fork": False, "archived": False, "stargazers_count": 5,
               "pushed_at": "2026-08-09T00:00:00Z", "language": "Python",
               "created_at": "2026-07-08T15:34:56Z", "open_issues": 526}
    cand = Candidate.from_issue("github-labels", "farm/repo",
                                {"number": 1, "title": "t", "labels": []})
    enriched = cand.with_repo_detail(payload)
    assert enriched.repo_created_at == "2026-07-08T15:34:56Z"
    assert enriched.repo_open_issues == 526
    assert enriched.created_at != enriched.repo_created_at
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_models.py -q -k repo_detail_captures`
Expected: FAIL — `AttributeError: 'Candidate' object has no attribute 'repo_created_at'`

- [ ] **Step 3: Capture the two fields**

In `sniper/models.py`, add to the defaults block beside `stars`/`pushed_at`:

```python
    repo_created_at: str | None = None
    repo_open_issues: int | None = None
```

And in `with_repo_detail`, add to the `replace(...)` call:

```python
            repo_created_at=payload.get("created_at"),
            repo_open_issues=payload.get("open_issues"),
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_models.py -q -k repo_detail_captures`
Expected: PASS.

- [ ] **Step 5: Write the failing `farm_penalty` tests**

Append to `tests/test_score.py`:

First extend `tests/test_score.py`'s `_cand` helper (line 49) so the repo-detail payload can carry the two new fields. Everything already calling it keeps working, because both default to absent:

```python
def _cand(*, amount_label="$1000", comments=0, stars=1000, pushed_days=1,
          repo_age_days=None, repo_open_issues=None):
    payload = {
        "number": 7, "title": "t", "state": "open", "comments": comments,
        "created_at": _at(400), "updated_at": _at(1), "html_url": "u",
        "repository_url": "https://api.github.com/repos/acme/widgets",
        "assignee": None, "labels": [{"name": amount_label}],
    }
    detail = {"fork": False, "archived": False, "stargazers_count": stars,
              "pushed_at": _at(pushed_days), "language": "Python"}
    if repo_age_days is not None:
        detail["created_at"] = _at(repo_age_days)
    if repo_open_issues is not None:
        detail["open_issues"] = repo_open_issues
    return Candidate.from_search_item("github-labels", payload).with_repo_detail(detail)
```

Then append:

```python
# The measured shape of zhangjiayang6835-cyber/bounty-plaza on 2026-08-09:
# created 2026-07-08 (32 days), 5 stars, 526 open issues, 0 bounties ever
# closed. It held three of the top four digest slots.
def test_all_three_farm_signals_sink_the_score_hard():
    cand = _cand(stars=5, repo_age_days=32, repo_open_issues=526)
    assert scoring.farm_penalty(cand, now=NOW) == pytest.approx(0.25)


def test_two_signals_sink_it_gently():
    cand = _cand(stars=5, repo_age_days=2000, repo_open_issues=526)
    assert scoring.farm_penalty(cand, now=NOW) == pytest.approx(0.6)


def test_one_signal_is_not_a_farm():
    cand = _cand(stars=5, repo_age_days=2000, repo_open_issues=12)
    assert scoring.farm_penalty(cand, now=NOW) == pytest.approx(1.0)


def test_a_young_well_starred_project_is_not_a_farm():
    # A funded launch looks nothing like bounty-plaza and must not be punished
    # for being new.
    cand = _cand(stars=4000, repo_age_days=10, repo_open_issues=400)
    assert scoring.farm_penalty(cand, now=NOW) == pytest.approx(1.0)


def test_an_unenriched_repo_takes_no_penalty():
    # Missing fields mean the detail call never returned them, not that the repo
    # is clean. Penalising absent data punishes a failed API call, not a farm.
    cand = _cand(stars=5)
    assert cand.repo_created_at is None
    assert scoring.farm_penalty(cand, now=NOW) == pytest.approx(1.0)


def test_the_penalty_multiplies_into_the_score():
    cand = _cand(amount_label="$200", comments=0, stars=5, pushed_days=1,
                 repo_age_days=32, repo_open_issues=526)
    # 200 / 1 * repo_health(5 stars, pushed yesterday) * 0.25
    expected = 200 * scoring.repo_health(5, _at(1), now=NOW) * 0.25
    assert scoring.score(cand, now=NOW) == pytest.approx(expected)
```

- [ ] **Step 6: Run to verify it fails**

Run: `python3 -m pytest tests/test_score.py -q -k farm`
Expected: FAIL — `AttributeError: module 'sniper.score' has no attribute 'farm_penalty'`

- [ ] **Step 7: Implement `farm_penalty`**

Add to `sniper/score.py`, above `score()`:

```python
# Farm shape, calibrated against the one measured example rather than invented.
# zhangjiayang6835-cyber/bounty-plaza on 2026-08-09: created 2026-07-08 (32 days),
# 5 stars, 526 open issues, 0 bounties ever closed. It held three of the top four
# digest slots.
YOUNG_REPO_DAYS = 90
FEW_STARS = 20
HUGE_BACKLOG = 200


def farm_penalty(cand, *, now: float | None = None) -> float:
    """Multiplier in {1.0, 0.6, 0.25} on how much this repo smells like a farm.

    A penalty, never a reject. payers.payout_reject already removes the clear
    cases on evidence -- a repo advertising a pile of bounties and never closing
    one. This catches the shape that survives it: a young, unstarred repo with a
    huge open backlog that has happened to close something. Sinking it keeps it
    visible so the operator can judge, which rejecting would not.

    Missing fields score 1.0. They mean enrichment never ran, not that the repo
    is clean, and penalising absent data would punish a failed API call.
    """
    if cand.repo_created_at is None or cand.repo_open_issues is None:
        return 1.0
    if cand.stars is None:
        return 1.0

    if cand.stars >= FEW_STARS:
        # A funded launch can be young with a big backlog of its own filed
        # issues and still be nothing like bounty-plaza (5 stars). Stars are
        # the signal that tells a farm from a generously young project, so
        # without it the other two -- alone or together -- do not sink the
        # score; "young, unstarred, huge backlog" is a conjunction, not a
        # majority vote among three independent flags.
        return 1.0

    age_days = (_now_utc(now) - parse_iso(cand.repo_created_at)).days
    # +1 for few_stars, already established by the early return above.
    signals = 1 + sum((age_days < YOUNG_REPO_DAYS,
                       cand.repo_open_issues > HUGE_BACKLOG))
    if signals == 3:
        return 0.25
    if signals == 2:
        return 0.6
    return 1.0
```

> **Corrected 2026-08-10, after this plan shipped.** The version originally written
> here scored the three signals as a majority vote, which contradicted this task's own
> `test_a_young_well_starred_project_is_not_a_farm` — 4000 stars, 10 days old, 400 open
> issues trips two of three signals, so the vote returned 0.6 where the test asserts 1.0.
> The implementer surfaced the contradiction rather than picking a side. The operator
> ruled that stars are a **gate**, not a vote: a repo the crowd has starred is never
> treated as a farm, whatever its age or backlog. Accepted hole: a farm that buys 20
> stars escapes the penalty. `payers.payout_reject` still rejects it on closed-bounty
> evidence, and `DIGEST_MAX_PER_REPO` caps any repo at three digest slots.

In `score()`, multiply it in:

```python
    competition = 1 + cand.comments + 2 * (cand.open_pr_count or 0)
    return (cand.amount_usd / competition
            * repo_health(cand.stars, cand.pushed_at, now=now)
            * farm_penalty(cand, now=now))
```

Update the module docstring's formula to `score = amount_usd / (1 + comments + 2 x open_prs) x repo_health x farm_penalty`.

- [ ] **Step 8: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add sniper/models.py sniper/score.py tests/
git commit -m "feat: a farm-shaped repo sinks instead of vanishing

payout_reject abstains as soon as a repo has closed even one bounty, so a farm
that occasionally closes something survives it. Three signals already present in
the repo-detail payload -- repo younger than 90 days, fewer than 20 stars, more
than 200 open issues -- sink the score to a quarter when all three hold and to
0.6 when two do.

Calibrated against the one repo actually measured rather than invented:
bounty-plaza was 32 days old with 5 stars and 526 open issues.

A penalty, not a reject, because metadata cannot tell a farm from a generous
young project and the operator can. Missing fields score 1.0: they mean
enrichment never ran, and penalising absent data punishes a failed API call
rather than a bad repo."
```

---

### Task 7: Render the whole inventory as markdown

The digest's own footer says "529 more open and unclaimed, but with no amount readable ... not shown", and there is no way to look at them. This task is the renderer only; publishing is Task 8.

**Files:**
- Create: `sniper/board.py`
- Modify: `sniper/store.py` (add `held_back_candidates`, `rejection_counts`)
- Test: `tests/test_board.py` (create), `tests/test_store_candidates.py` (append)

**Interfaces:**
- Consumes: `sqlite3.Row` objects shaped like the `candidates` table; `notify.format_row` for the rankable lines.
- Produces:
  - `store.held_back_candidates(conn, limit: int) -> list[sqlite3.Row]` — live, `amount_usd IS NULL`, newest `first_seen_at` first
  - `store.rejection_counts(conn) -> list[tuple[str, int]]` — reason and count, largest first
  - `board.render(rankable, unrankable, rejection_counts, *, now, warnings, totals) -> str`

- [ ] **Step 1: Write the failing store tests**

Append to `tests/test_store_candidates.py`:

```python
def test_held_back_candidates_returns_only_the_amountless_live_ones(tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    priced = replace(_candidate(number=1), amount_usd=100)
    unpriced = replace(_candidate(number=2), amount_usd=None)
    rejected = replace(_candidate(number=3), amount_usd=None)
    store.upsert_candidate(conn, priced, now=1.0)
    store.upsert_candidate(conn, unpriced, now=2.0)
    store.upsert_candidate(conn, rejected, rejected="fork", now=3.0)

    rows = store.held_back_candidates(conn, 10)

    assert [row["number"] for row in rows] == [2]


def test_held_back_candidates_are_newest_first(tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    for number, seen in ((1, 10.0), (2, 30.0), (3, 20.0)):
        store.upsert_candidate(conn, replace(_candidate(number=number),
                                             amount_usd=None), now=seen)
    rows = store.held_back_candidates(conn, 10)
    assert [row["number"] for row in rows] == [2, 3, 1]


def test_rejection_counts_group_by_reason_largest_first(tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    store.upsert_candidate(conn, _candidate(number=1), rejected="fork", now=1.0)
    store.upsert_candidate(conn, _candidate(number=2), rejected="archived repo", now=1.0)
    store.upsert_candidate(conn, _candidate(number=3), rejected="archived repo", now=1.0)
    store.upsert_candidate(conn, _candidate(number=4), now=1.0)

    assert store.rejection_counts(conn) == [("archived repo", 2), ("fork", 1)]
```

Add `from dataclasses import replace` to that test module's imports.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_store_candidates.py -q -k "held_back or rejection_counts"`
Expected: FAIL — `AttributeError: module 'sniper.store' has no attribute 'held_back_candidates'`

- [ ] **Step 3: Add the two queries**

Append to `sniper/store.py`:

```python
def held_back_candidates(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Live candidates with no readable amount, newest first.

    These are the rows `live_candidates(require_amount=True)` excludes -- 529 of
    them on 2026-08-09. They are excluded from the DIGEST for a good reason: they
    cannot be ranked by expected value, and once the rankable ones ran out they
    padded it with 700-day-old issues titled "test" and "tttt". But excluded from
    the digest is not the same as invisible, and until the board existed there
    was no way to look at them at all.

    Ordered by first_seen_at, our own clock, because there is no amount to sort
    on and GitHub's created_at is meaningless for a label applied years later.
    """
    return conn.execute(
        "SELECT * FROM candidates WHERE rejected IS NULL AND amount_usd IS NULL "
        "ORDER BY first_seen_at DESC LIMIT ?", (limit,)).fetchall()


def rejection_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Every reject reason and how many candidates carry it, largest first.

    This is how a wrong filter becomes visible. 'open linked PR' sat at 562 --
    larger than everything the tracker kept -- for as long as nobody ran this
    query by hand.
    """
    return [(row["rejected"], row["n"]) for row in conn.execute(
        "SELECT rejected, COUNT(*) AS n FROM candidates "
        "WHERE rejected IS NOT NULL GROUP BY rejected ORDER BY n DESC, rejected")]
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_store_candidates.py -q -k "held_back or rejection_counts"`
Expected: PASS.

- [ ] **Step 5: Write the failing renderer tests**

Create `tests/test_board.py`:

```python
"""The board: everything the digest cannot say.

The digest is a shopping list -- short, ranked, delivered on a schedule. The
board is the whole table, and it exists because two things were invisible: the
529 live candidates with no readable amount, and the reject reasons, one of
which had quietly grown larger than everything being kept.
"""

from sniper import board


def _row(**kwargs):
    base = {"repo": "acme/widgets", "number": 1, "title": "t", "url": "u",
            "amount_usd": 100, "comments": 0, "language": "Python",
            "created_at": "2026-08-01T00:00:00Z", "open_pr_count": None}
    base.update(kwargs)
    return base


_NOW = 1786276800.0  # 2026-08-09T12:00:00Z  (corrected 2026-08-10: the literal
                     # first written here was 1786363200.0, which is 2026-08-10,
                     # one day off from its own comment and from the assertion
                     # below that the footer carries 2026-08-09)


def test_rankable_rows_are_listed_with_their_amounts():
    text = board.render([_row(amount_usd=250, number=7)], [], [],
                        now=_NOW, warnings=[], totals={})
    assert "$250" in text
    assert "acme/widgets#7" in text


def test_open_pr_count_is_shown_as_competition_not_hidden():
    text = board.render([_row(open_pr_count=2)], [], [],
                        now=_NOW, warnings=[], totals={})
    assert "2 PRs" in text


def test_no_pr_marker_when_there_are_none():
    text = board.render([_row(open_pr_count=0)], [], [],
                        now=_NOW, warnings=[], totals={})
    assert "PRs" not in text


def test_unrankable_rows_get_their_own_section_grouped_by_repo():
    rows = [_row(repo="a/one", number=1, amount_usd=None),
            _row(repo="a/one", number=2, amount_usd=None),
            _row(repo="b/two", number=3, amount_usd=None)]
    text = board.render([], rows, [], now=_NOW, warnings=[], totals={})
    assert "No readable amount" in text
    assert text.count("a/one") >= 1
    assert "#1" in text and "#2" in text and "#3" in text
    # grouped: the repo heading appears once, not once per issue
    assert text.count("### a/one") == 1


def test_rejections_are_listed_by_reason_largest_first():
    text = board.render([], [], [("open linked PR", 562), ("fork", 4)],
                        now=_NOW, warnings=[], totals={})
    assert text.index("open linked PR") < text.index("fork")
    assert "562" in text


def test_warnings_render_above_everything():
    text = board.render([_row()], [], [], now=_NOW,
                        warnings=["payout history is partial"], totals={})
    assert text.index("payout history is partial") < text.index("acme/widgets")


def test_an_empty_board_says_so_rather_than_rendering_nothing():
    # A blank page and "the sweep found nothing" must not look identical.
    text = board.render([], [], [], now=_NOW, warnings=[], totals={})
    assert "nothing" in text.lower()


def test_the_footer_carries_the_sweep_time_and_totals():
    text = board.render([], [], [], now=_NOW, warnings=[],
                        totals={"live": 536, "held_back": 529, "rejected": 970})
    assert "2026-08-09" in text
    assert "536" in text and "529" in text and "970" in text
```

- [ ] **Step 6: Run to verify it fails**

Run: `python3 -m pytest tests/test_board.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sniper.board'`

- [ ] **Step 7: Write `sniper/board.py`**

```python
"""The bounty board: the whole inventory, rendered as one markdown page.

The digest is a shopping list -- short, ranked, pushed on a schedule, and
deliberately silent when there is nothing new, because 144 "nothing found"
messages a day would train the reader to ignore the channel.

The board is the opposite: everything, always, at a stable URL. It exists
because two things were invisible from the digest alone.

  * 529 live candidates carried no readable amount. They are correctly excluded
    from a ranked digest -- expected value cannot be computed without a price,
    and once the rankable ones ran out they padded it with 700-day-old issues
    titled "test" and "tttt". But "not rankable" became "unreachable", and the
    digest's own footer said so every three hours with no way to act on it.

  * The reject reasons were never displayed anywhere. 'open linked PR' had
    quietly grown to 562 -- larger than everything the tracker was keeping --
    and only a hand-written SQL query against the production database found it.

Rendering is a pure function of rows so it can be tested without a database, a
network, or a filesystem.
"""

from __future__ import annotations

from . import notify
from .models import _now_utc, parse_iso

TITLE_CHARS = 90


def _get(row, key):
    """Read a column from either a sqlite3.Row or a plain dict.

    sqlite3.Row raises IndexError for an unknown column and dict raises
    KeyError, so one helper covers both and the renderer stays testable without
    a database.
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _fmt_row(row, *, now: float | None) -> str:
    """One board line. Mirrors notify.format_row, plus the competition marker."""
    line = notify.format_row(row, now=now)
    count = _get(row, "open_pr_count")
    if count:
        line += f" · ⚠ {count} PRs"
    return line


def _unrankable_line(row, *, now: float | None) -> str:
    title = row["title"]
    if len(title) > TITLE_CHARS:
        title = title[: TITLE_CHARS - 1] + "…"
    age = (_now_utc(now) - parse_iso(row["created_at"])).days
    return f"- [#{row['number']}]({row['url']}) {title} — {row['comments']}💬 · {age}d"


def render(rankable, unrankable, rejection_counts, *, now: float | None,
           warnings, totals: dict[str, int]) -> str:
    """The whole board. Warnings first, then priced, then unpriced, then rejects."""
    parts: list[str] = ["# Bounty board", ""]

    for warning in warnings:
        parts.append(f"> ⚠ {warning}")
    if warnings:
        parts.append("")

    if not rankable and not unrankable and not rejection_counts:
        parts.append("_The last sweep found nothing at all — no live candidates, "
                     "and nothing rejected. That is a sweep that ran and came back "
                     "empty, not a sweep that failed; check watch.log for liveness._")
        parts.append("")

    if rankable:
        parts.append(f"## Rankable — {len(rankable)} with a readable amount")
        parts.append("")
        parts.extend(_fmt_row(row, now=now) for row in rankable)
        parts.append("")

    if unrankable:
        parts.append(f"## No readable amount — {len(unrankable)} live, unranked")
        parts.append("")
        parts.append("_No price in the labels, title or body, so expected value "
                     "cannot be computed and the digest will never show them. "
                     "Grouped by repo, most recently discovered first._")
        parts.append("")
        by_repo: dict[str, list] = {}
        for row in unrankable:
            by_repo.setdefault(row["repo"], []).append(row)
        for repo, rows in by_repo.items():
            parts.append(f"### {repo}")
            parts.extend(_unrankable_line(row, now=now) for row in rows)
            parts.append("")

    if rejection_counts:
        parts.append("## Rejected")
        parts.append("")
        parts.append("_Why candidates were dropped. A reason that grows larger "
                     "than the kept pile is a filter to go and read._")
        parts.append("")
        parts.append("| count | reason |")
        parts.append("| ----: | ------ |")
        for reason, count in rejection_counts:
            parts.append(f"| {count} | {reason} |")
        parts.append("")

    swept = _now_utc(now).strftime("%Y-%m-%d %H:%M UTC")
    summary = " · ".join(f"{key} {value}" for key, value in totals.items())
    parts.append("---")
    parts.append(f"_Swept {swept}{' · ' + summary if summary else ''}_")
    return "\n".join(parts)
```

- [ ] **Step 8: Run to verify it passes**

Run: `python3 -m pytest tests/test_board.py -q`
Expected: PASS, 8 tests.

- [ ] **Step 9: Run the full suite and commit**

Run: `python3 -m pytest -q`

```bash
git add sniper/board.py sniper/store.py tests/test_board.py tests/test_store_candidates.py
git commit -m "feat: render the whole inventory, including the part the digest hides

The digest is a shopping list and is deliberately short. That left two things
with no way to see them at all.

529 live candidates carry no readable amount. Excluding them from a ranked digest
is right -- expected value needs a price, and without one they padded it with
700-day-old issues titled 'test' and 'tttt' -- but 'not rankable' had become
'unreachable', and the digest said so every three hours with no way to act on it.

And the reject reasons were displayed nowhere. 'open linked PR' had grown to 562,
larger than everything being kept, and only a hand-written query against the
production database found it. The board now shows every reason and its count, so
the next wrong filter surfaces in days."
```

---

### Task 8: Publish the board to a private gist, and link it when it changes

**Files:**
- Modify: `sniper/board.py` (add `publish`, `create_gist`, `signature`, `has_changed`)
- Modify: `sniper/config.py` (add `BOUNTY_GIST_ID`, `BOARD_PATH`, `BOARD_UNRANKABLE_N`, `BOARD_CHANGE_PCT`)
- Modify: `hunt.py` (write the file, publish, link on change)
- Test: `tests/test_board.py` (append)

**Interfaces:**
- Consumes: `store.held_back_candidates`, `store.rejection_counts`, `store.live_candidates`, `store.get_cache`, `store.set_cache`.
- Produces:
  - `board.publish(text: str, *, post=None) -> str | None` — returns the gist URL, or None when no gist is configured
  - `board.create_gist(text: str, *, post=None) -> tuple[str, str]` — `(gist_id, url)`
  - `board.signature(unrankable_rows) -> dict` — `{"count": int, "repos": sorted list}`
  - `board.has_changed(previous: dict | None, current: dict, *, pct: float) -> bool`

- [ ] **Step 1: Write the failing change-detection tests**

Append to `tests/test_board.py`:

```python
def test_a_first_ever_board_counts_as_changed():
    assert board.has_changed(None, {"count": 5, "repos": ["a/b"]}, pct=5.0) is True


def test_a_new_repo_in_the_unrankable_set_counts_as_changed():
    before = {"count": 100, "repos": ["a/b"]}
    after = {"count": 100, "repos": ["a/b", "c/d"]}
    assert board.has_changed(before, after, pct=5.0) is True


def test_a_small_count_drift_is_not_worth_a_message():
    # 144 links a day trains the reader to ignore the channel, which is the
    # failure the digest's own silence rule already guards against.
    before = {"count": 529, "repos": ["a/b"]}
    after = {"count": 540, "repos": ["a/b"]}   # +2.1%
    assert board.has_changed(before, after, pct=5.0) is False


def test_a_large_count_move_is_worth_a_message():
    before = {"count": 529, "repos": ["a/b"]}
    after = {"count": 600, "repos": ["a/b"]}   # +13.4%
    assert board.has_changed(before, after, pct=5.0) is True


def test_a_set_emptying_out_is_always_worth_a_message():
    before = {"count": 529, "repos": ["a/b"]}
    after = {"count": 0, "repos": []}
    assert board.has_changed(before, after, pct=5.0) is True


def test_signature_records_the_count_and_the_repos():
    rows = [_row(repo="b/two", amount_usd=None), _row(repo="a/one", amount_usd=None),
            _row(repo="b/two", amount_usd=None)]
    assert board.signature(rows) == {"count": 3, "repos": ["a/one", "b/two"]}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_board.py -q -k "changed or signature"`
Expected: FAIL — `AttributeError: module 'sniper.board' has no attribute 'has_changed'`

- [ ] **Step 3: Implement change detection**

Append to `sniper/board.py`:

```python
def signature(unrankable_rows) -> dict:
    """A cheap fingerprint of the unrankable set: how many, and from where."""
    repos = sorted({row["repo"] for row in unrankable_rows})
    return {"count": len(unrankable_rows), "repos": repos}


def has_changed(previous: dict | None, current: dict, *, pct: float) -> bool:
    """Is this board worth putting a link in front of the operator for?

    The digest already refuses to say "nothing new" every ten minutes, for a
    reason that applies here too: a channel that speaks constantly stops being
    read, and the one message that matters then arrives somewhere nobody looks.

    So a link is worth sending when a repo appears that was not there before --
    genuinely new inventory -- or when the count moves by more than `pct`.
    Everything else is drift.
    """
    if previous is None:
        return True
    if set(current["repos"]) - set(previous.get("repos") or []):
        return True
    before = previous.get("count") or 0
    if before == 0:
        return current["count"] > 0
    return abs(current["count"] - before) / before * 100.0 > pct
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_board.py -q -k "changed or signature"`
Expected: PASS, 6 tests.

- [ ] **Step 5: Write the failing publishing tests**

```python
class _FakePost:
    def __init__(self, status=200, body=None):
        self.calls = []
        self._status = status
        self._body = body or {"html_url": "https://gist.github.com/abc123"}

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))

        class Resp:
            status_code = self._status
            def json(_self):
                return self._body
            def raise_for_status(_self):
                if self._status >= 400:
                    raise AssertionError(f"HTTP {self._status}")
        return Resp()


def test_publish_patches_the_configured_gist(monkeypatch):
    from sniper import config
    monkeypatch.setattr(config, "BOUNTY_GIST_ID", "abc123", raising=False)
    post = _FakePost()
    url = board.publish("# board", post=post)
    called_url, payload, _ = post.calls[0]
    assert called_url == "https://api.github.com/gists/abc123"
    assert payload["files"]["bounties.md"]["content"] == "# board"
    assert url == "https://gist.github.com/abc123"


def test_publish_returns_none_when_no_gist_is_configured(monkeypatch):
    from sniper import config
    monkeypatch.setattr(config, "BOUNTY_GIST_ID", "", raising=False)
    post = _FakePost()
    assert board.publish("# board", post=post) is None
    assert post.calls == []


def test_publish_never_creates_a_second_gist(monkeypatch):
    # The whole point of one gist is a link that keeps working. Creating a new
    # one each sweep would hand the operator a dead bookmark every ten minutes.
    from sniper import config
    monkeypatch.setattr(config, "BOUNTY_GIST_ID", "abc123", raising=False)
    post = _FakePost()
    board.publish("# board", post=post)
    assert all("/gists/abc123" in url for url, _, _ in post.calls)


def test_create_gist_makes_it_private(monkeypatch):
    post = _FakePost(body={"id": "newid", "html_url": "https://gist.github.com/newid"})
    gist_id, url = board.create_gist("# board", post=post)
    _, payload, _ = post.calls[0]
    assert payload["public"] is False
    assert gist_id == "newid"
    assert url == "https://gist.github.com/newid"
```

- [ ] **Step 6: Run to verify it fails, then implement publishing**

Run: `python3 -m pytest tests/test_board.py -q -k "publish or create_gist"`
Expected: FAIL — `AttributeError: module 'sniper.board' has no attribute 'publish'`

`publish` uses `requests.patch` and `create_gist` uses `requests.post`; the `post=` parameter injects whichever one the function would have used, so no test reaches the network. Append to `sniper/board.py`:

```python
import requests

GIST_FILENAME = "bounties.md"
GIST_API = "https://api.github.com/gists"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def create_gist(text: str, *, post=None) -> tuple[str, str]:
    """Create the private gist this board lives in. Returns (id, url).

    Run once by hand via `hunt.py --init-gist`; the id then goes in .env as
    BOUNTY_GIST_ID so every later sweep updates the SAME url. A gist created per
    sweep would hand the operator a bookmark that dies every ten minutes.
    """
    poster = requests.post if post is None else post
    resp = poster(GIST_API, json={
        "description": "expensify-sniper bounty board",
        "public": False,
        "files": {GIST_FILENAME: {"content": text}},
    }, headers=_headers(), timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    return payload["id"], payload["html_url"]


def publish(text: str, *, post=None) -> str | None:
    """Update the configured gist. Returns its url, or None if none is set.

    None is a real answer and the caller must say it out loud. An unset gist id
    silently succeeding is the same failure mode as an unset Discord webhook:
    every run reports success while nothing reaches anybody.
    """
    if not config.BOUNTY_GIST_ID:
        return None
    patcher = requests.patch if post is None else post
    resp = patcher(f"{GIST_API}/{config.BOUNTY_GIST_ID}",
                   json={"files": {GIST_FILENAME: {"content": text}}},
                   headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()["html_url"]
```

Add `from . import config, notify` at the top of `board.py`, replacing the existing `from . import notify`.

- [ ] **Step 7: Run to verify it passes**

Run: `python3 -m pytest tests/test_board.py -q`
Expected: PASS.

- [ ] **Step 8: Add the config entries**

In `sniper/config.py`, beside the other delivery settings:

```python
# The board lives in ONE private gist so the operator has a stable bookmark.
# Create it once with `python3 hunt.py --init-gist` and paste the id here.
BOUNTY_GIST_ID = os.environ.get("BOUNTY_GIST_ID", "").strip()
BOARD_PATH = Path(os.environ.get("BOARD_PATH", ROOT / "bounties.md"))
# How many amountless candidates the board lists. 529 on 2026-08-09, so this is
# a page-size guard rather than a filter; the footer says when it truncates.
BOARD_UNRANKABLE_N = int(os.environ.get("BOARD_UNRANKABLE_N", "600"))
# Percentage move in the unrankable count that is worth a Discord link.
BOARD_CHANGE_PCT = float(os.environ.get("BOARD_CHANGE_PCT", "5"))
```

- [ ] **Step 9: Wire it into `hunt.py`**

In `hunt.run()`, after the digest text is assembled (after the `text = notify.format_digest(...)` call at line 244) and BEFORE the `nothing_to_say` check:

```python
    # The board: everything, always, at a stable url. Written before the
    # "nothing to say" short-circuit, because the board is the thing that must
    # NOT go quiet -- it is where the 529 unrankable candidates and the reject
    # reasons live, and both matter most on a sweep with no news.
    unrankable = store.held_back_candidates(conn, config.BOARD_UNRANKABLE_N)
    rejections = store.rejection_counts(conn)
    board_text = board.render(
        store.live_candidates(conn, config.DIGEST_TOP_N, require_amount=True),
        unrankable,
        rejections,
        now=stamp, warnings=warnings,
        totals={"live": len(new_rows) + len(standing_rows),
                "unranked": held_back,
                "rejected": sum(count for _, count in rejections)})
    try:
        config.BOARD_PATH.write_text(board_text, encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        warnings.append(f"could not write {config.BOARD_PATH}: {exc}")

    board_url = None
    if not dry_run:
        try:
            board_url = board.publish(board_text)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"board publish failed: {exc}")
        if board_url is None and not config.BOUNTY_GIST_ID:
            print("⚠ board not published: BOUNTY_GIST_ID unset "
                  "(run `python3 hunt.py --init-gist` once)", file=sys.stderr)

    # Link the board only when the unrankable set actually moved. Sending it
    # every sweep is 144 links a day, which trains the reader to ignore the
    # channel -- the exact failure the digest's own silence rule guards against.
    current = board.signature(unrankable)
    _, previous_body = store.get_cache(conn, "board-signature")
    try:
        previous = json.loads(previous_body) if previous_body else None
    except ValueError:
        previous = None
    if board_url and board.has_changed(previous, current,
                                       pct=config.BOARD_CHANGE_PCT):
        text += f"\n\n📋 board updated: {board_url}"
    store.set_cache(conn, "board-signature", None, json.dumps(current))
```

Add `board` to the `from sniper import ...` block at the top of `hunt.py`, and `import json` if it is not already imported.

- [ ] **Step 10: Add `--init-gist`**

In `hunt.main()`, add the argument beside the others:

```python
    parser.add_argument("--init-gist", action="store_true",
                        help="create the private gist the board lives in, once, "
                             "and print the id to put in .env as BOUNTY_GIST_ID")
```

and handle it before the run, immediately after `args = parser.parse_args(argv)`:

```python
    if args.init_gist:
        gist_id, url = board.create_gist("# Bounty board\n\n_not swept yet_\n")
        print(f"created {url}\n\nadd this line to .env on BOTH hosts:\n"
              f"BOUNTY_GIST_ID={gist_id}")
        return 0
```

- [ ] **Step 11: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS.

- [ ] **Step 12: Verify the board renders against real data, without publishing**

Run: `python3 hunt.py --dry-run`
Expected: exits 0, writes `bounties.md`, publishes nothing. Then check the file has all three sections:

Run: `grep -c '^## ' bounties.md`
Expected: `3`

- [ ] **Step 13: Commit**

```bash
git add sniper/board.py sniper/config.py hunt.py tests/test_board.py
git commit -m "feat: publish the board to one private gist, and link it when it moves

Every sweep writes bounties.md and PATCHes a single private gist, so the operator
has a stable bookmark rather than a new dead link every ten minutes. The id is
created once by 'hunt.py --init-gist' and lives in .env; an unset id warns on
stderr instead of succeeding silently, which is the failure mode an unset Discord
webhook already taught us.

The board is written BEFORE the digest's 'nothing to say' short-circuit. It is
the thing that must not go quiet: the unrankable candidates and the reject counts
matter most on a sweep with no news.

Discord gets the link only when a new repo appears in the unrankable set or its
count moves more than 5%. A channel that speaks every ten minutes stops being
read, and then the one message that matters arrives somewhere nobody looks."
```

---

## Deployment

- [ ] **Step 1: Merge to the tracker branch**

```bash
git checkout bounty-tracker
git merge bounty-tracker-impl
```

- [ ] **Step 2: Create the gist and record its id**

```bash
python3 hunt.py --init-gist
```

Add the printed `BOUNTY_GIST_ID=...` line to `.env` on **both** hosts (local and `ssh openclaw`). `.env` is chmod 600 and outside any git repo — never commit it.

- [ ] **Step 3: Deploy to OCI**

```bash
rsync -av --exclude='.git' --exclude='.venv' --exclude='*.sqlite' \
    ~/Documents/expensify-sniper/ openclaw:~/expensify-sniper/
```

- [ ] **Step 4: Verify the migration lands on the production database**

```bash
ssh openclaw "sqlite3 ~/expensify-sniper/sniper.sqlite 'PRAGMA table_info(candidates)'" | grep open_pr_count
```
Expected: one line containing `open_pr_count|INTEGER`. If it is absent, `store.connect` has not run yet — run `ssh openclaw "cd ~/expensify-sniper && python3 hunt.py --dry-run"` and check again.

- [ ] **Step 5: Confirm the 562 were re-judged**

```bash
ssh openclaw "sqlite3 ~/expensify-sniper/sniper.sqlite \
  'select ifnull(rejected,\"KEPT\"), count(*) from candidates group by 1 order by 2 desc limit 10'"
```
Expected: no `open linked PR` row remains; the `KEPT` count has risen substantially from 536.

- [ ] **Step 6: Confirm the farm is gone from the digest**

```bash
ssh openclaw "cd ~/expensify-sniper && python3 hunt.py --dry-run" | head -30
```
Expected: `zhangjiayang6835-cyber/bounty-plaza` no longer holds the top slots. If it still does, check whether `ever_closed_with` returned `None` (abstain) rather than 0 — a throttled search reads as unknown by design, so re-run after a minute before concluding the rule is wrong.

---

## Self-Review

**Spec coverage**

| spec section | task |
| ------------ | ---- |
| A1 closed-bounty reject, label-agnostic | Task 5 |
| A1 `bounty_label_of`, `ever_closed_with` returning None | Task 5 steps 1-8 |
| A1 24h cache under `closed-bounty:<repo>:<label>` | Task 5 step 7 |
| A2 quality downrank, `created_at` + `open_issues` capture | Task 6 |
| B `LinkedPRs`, `linked_pr_state` | Task 1 |
| B `assignees` on Candidate | Task 1 step 7 |
| B competition term `1 + comments + 2*prs` | Task 3 steps 1-4 |
| B `open_pr_count` column + migration | Task 2 |
| B `clear_rejections` for the 562 | Task 2 + Task 3 step 8 |
| C source-aware assignee reject | Task 4 |
| D `board.render` with all four sections | Task 7 |
| D gist publish, `--init-gist`, unset-id warning | Task 8 |
| D Discord link on material change | Task 8 steps 1-4, 9 |
| D no-network tests via injected seam | Task 8 step 5 |

**Deviations from the spec, deliberate:**

1. The spec said `has_assignee` becomes a property derived from `assignees`. The plan keeps it as a field and adds `assignees` alongside. `Candidate` is a frozen dataclass constructed by name across the whole suite; converting a mid-list field to a property would churn every construction site for no behavioural gain. Both are populated from the same payload in one place, so they cannot disagree.
2. The spec described `apply_payout_rejections` keeping its `algora_label` argument. It cannot: with the label gate gone it needs a closed-count per repo, so it takes a lazy `closed_for(repo, label)` callback instead. Lazy matters — asking about every repo in the table would spend the 30/min search pool on rows that were never going to be rejected.
