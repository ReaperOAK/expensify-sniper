"""The candidates table.

bids.issue is an INTEGER PRIMARY KEY -- a single-repo assumption that collides
the moment a second source appears. candidates is keyed on (source, repo,
number) instead. The old tables are left alone so verify.py keeps working.

first_seen_at is OUR freshness clock. It is meaningless on run 1 and accurate
forever after, which is exactly why GitHub's created_at is displayed but never
scored.
"""

from dataclasses import replace

from sniper import store
from sniper.models import Candidate


def _conn(tmp_path):
    return store.connect(tmp_path / "t.sqlite")


def _candidate(*, number: int, source: str = "github-labels",
               repo: str = "acme/widgets") -> Candidate:
    return Candidate(
        source=source, repo=repo, number=number, title="t",
        url=f"https://github.com/{repo}/issues/{number}", state="open",
        comments=0, labels=(), created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z", has_assignee=False,
        amount_usd=100, amounts_seen=(100,))


def _cand(number=7, *, updated="2026-08-01T00:00:00Z", source="github-labels"):
    return Candidate.from_search_item(source, {
        "number": number, "title": f"issue {number}", "state": "open", "comments": 2,
        "created_at": "2026-01-01T00:00:00Z", "updated_at": updated,
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
        "repository_url": "https://api.github.com/repos/acme/widgets",
        "assignee": None, "labels": [{"name": "$780"}],
    }).with_repo_detail({"fork": False, "archived": False, "stargazers_count": 900,
                         "pushed_at": "2026-08-01T00:00:00Z", "language": "Python"})


def test_the_legacy_tables_still_exist(tmp_path):
    conn = _conn(tmp_path)
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"seen_events", "bids", "http_cache"} <= names
    assert {"candidates", "rate_budget"} <= names


def test_first_upsert_reports_new_and_the_second_does_not(tmp_path):
    conn = _conn(tmp_path)
    assert store.upsert_candidate(conn, _cand().with_score(10.0), now=100.0) is True
    assert store.upsert_candidate(conn, _cand().with_score(11.0), now=200.0) is False


def test_first_seen_at_is_preserved_across_updates(tmp_path):
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand().with_score(10.0), now=100.0)
    store.upsert_candidate(conn, _cand().with_score(11.0), now=999.0)
    row = conn.execute("SELECT first_seen_at, last_seen_at, score FROM candidates").fetchone()
    assert row["first_seen_at"] == 100.0
    assert row["last_seen_at"] == 999.0
    assert row["score"] == 11.0


def test_the_same_number_in_two_sources_does_not_collide(tmp_path):
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand(7, source="github-labels"), now=1.0)
    store.upsert_candidate(conn, _cand(7, source="expensify"), now=1.0)
    assert store.candidate_count(conn) == 2


def test_known_updated_at_round_trips_and_is_none_when_absent(tmp_path):
    conn = _conn(tmp_path)
    assert store.known_updated_at(conn, "github-labels", "acme/widgets", 7) is None
    store.upsert_candidate(conn, _cand(7, updated="2026-08-01T00:00:00Z"), now=1.0)
    assert store.known_updated_at(conn, "github-labels", "acme/widgets", 7) == \
        "2026-08-01T00:00:00Z"


def test_labels_round_trip_as_json(tmp_path):
    import json
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand(), now=1.0)
    row = conn.execute("SELECT labels FROM candidates").fetchone()
    assert json.loads(row["labels"]) == ["$780"]


def test_rejected_candidates_are_recorded_with_their_reason_not_dropped(tmp_path):
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand(8), rejected="fork", now=1.0)
    row = conn.execute("SELECT rejected FROM candidates WHERE number = 8").fetchone()
    assert row["rejected"] == "fork"


def test_live_candidates_excludes_rejects_and_sorts_by_score_descending(tmp_path):
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand(1).with_score(5.0), now=1.0)
    store.upsert_candidate(conn, _cand(2).with_score(50.0), now=1.0)
    store.upsert_candidate(conn, _cand(3).with_score(500.0), rejected="fork", now=1.0)
    assert [r["number"] for r in store.live_candidates(conn, limit=10)] == [2, 1]


def test_live_candidates_honours_the_limit(tmp_path):
    conn = _conn(tmp_path)
    for n in range(5):
        store.upsert_candidate(conn, _cand(n).with_score(float(n)), now=1.0)
    assert len(store.live_candidates(conn, limit=2)) == 2


def test_live_candidates_since_returns_only_newly_seen(tmp_path):
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand(1).with_score(1.0), now=100.0)
    store.upsert_candidate(conn, _cand(2).with_score(2.0), now=300.0)
    assert [r["number"] for r in store.live_candidates(conn, limit=10, since=200.0)] == [2]


def test_a_candidate_that_becomes_rejected_stops_being_live(tmp_path):
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand(1).with_score(1.0), now=100.0)
    assert len(store.live_candidates(conn, limit=10)) == 1
    store.upsert_candidate(conn, _cand(1).with_score(1.0), rejected="assigned", now=200.0)
    assert store.live_candidates(conn, limit=10) == []


def test_only_one_writer_reports_new_when_two_connections_race(tmp_path):
    """hunt.py and the Expensify watcher share this database.

    A read-then-write pair let both conclude "new" for the same key, because a
    bare SELECT takes no lock. Newness now comes from INSERT OR IGNORE's rowcount,
    so exactly one writer can win however they interleave.
    """
    a = _conn(tmp_path)
    b = store.connect(tmp_path / "t.sqlite")
    results = [
        store.upsert_candidate(a, _cand(42), now=100.0),
        store.upsert_candidate(b, _cand(42), now=100.0),
    ]
    assert results.count(True) == 1
    assert store.candidate_count(a) == 1


def test_an_unscored_candidate_sorts_below_every_scored_one(tmp_path):
    """A NULL score means "never enriched", not "worthless but rankable".

    SQLite puts NULL first under ORDER BY ... DESC in some orderings, which would
    float un-enriched candidates to the top of the digest.
    """
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand(1).with_score(5.0), now=1.0)
    store.upsert_candidate(conn, _cand(2), now=1.0)  # score stays None
    store.upsert_candidate(conn, _cand(3).with_score(50.0), now=1.0)
    assert [r["number"] for r in store.live_candidates(conn, limit=10)] == [3, 1, 2]


def test_no_single_repo_can_monopolise_the_digest(tmp_path):
    """Measured 2026-08-09: one repo held 188 live issues worth $917,000 and took
    all 25 digest slots, burying 26 other repos."""
    conn = _conn(tmp_path)
    for n in range(10):  # one loud repo, high scores
        store.upsert_candidate(conn, _cand(n).with_score(1000.0 - n), now=1.0)
    for n in range(3):   # quieter repos, lower scores
        c = Candidate.from_search_item("github-labels", {
            "number": 500 + n, "title": "t", "state": "open", "comments": 0,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-08-01T00:00:00Z",
            "html_url": "u", "repository_url": f"https://api.github.com/repos/other/r{n}",
            "assignee": None, "labels": [{"name": "$100"}],
        }).with_repo_detail({"fork": False, "archived": False, "stargazers_count": 9,
                             "pushed_at": "2026-08-01T00:00:00Z", "language": "Go"})
        store.upsert_candidate(conn, c.with_score(10.0), now=1.0)

    rows = store.live_candidates(conn, limit=6, max_per_repo=2)
    repos = [r["repo"] for r in rows]
    assert repos.count("acme/widgets") == 2
    assert len(set(repos)) == 4
    # Uncapped, the loud repo still wins outright -- the cap is the only defence.
    assert all(r["repo"] == "acme/widgets"
               for r in store.live_candidates(conn, limit=6))


def test_unrankable_candidates_do_not_take_a_digest_slot(tmp_path):
    """Measured 2026-08-09: 543 of 748 live candidates had no readable amount.

    They padded the digest with 700-day-old issues titled "test" and "tttt" once
    the handful of real ones ran out. They stay in the database -- hunt.py
    reports how many were held back -- but they do not displace something that
    can actually be ranked by expected value.
    """
    conn = _conn(tmp_path)
    store.upsert_candidate(conn, _cand(1).with_score(500.0), now=1.0)

    junk = Candidate.from_search_item("github-labels", {
        "number": 99, "title": "tttt", "state": "open", "comments": 4,
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2026-08-01T00:00:00Z",
        "html_url": "u", "repository_url": "https://api.github.com/repos/xeymxmkf/aaa",
        "assignee": None, "labels": [{"name": "bounty"}],
    }).with_repo_detail({"fork": False, "archived": False, "stargazers_count": 0,
                         "pushed_at": "2026-08-01T00:00:00Z", "language": None})
    assert junk.amount_usd is None
    store.upsert_candidate(conn, junk.with_score(0.0), now=1.0)

    assert len(store.live_candidates(conn, limit=10)) == 2
    ranked = store.live_candidates(conn, limit=10, require_amount=True)
    assert [r["number"] for r in ranked] == [1]


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


def test_live_counts_are_the_whole_table_not_a_page(tmp_path):
    """The board's headline numbers used to be page sizes.

    live_candidates and held_back_candidates both take a limit, so counting
    the rows they return reports how many were SHOWN. The board said
    "Rankable — 25" over an inventory of 48, and the footer said "live 30"
    while 982 rows were live.
    """
    conn = store.connect(tmp_path / "s.sqlite")
    for number in range(4):
        store.upsert_candidate(conn, _candidate(number=number), now=1.0)
    for number in range(4, 9):
        store.upsert_candidate(conn, replace(_candidate(number=number),
                                             amount_usd=None), now=1.0)
    store.upsert_candidate(conn, _candidate(number=99), rejected="fork", now=1.0)

    assert store.live_counts(conn) == {"live": 9, "rankable": 4, "unranked": 5}


def test_live_counts_on_an_empty_table_are_zero_not_none(tmp_path):
    # SUM over no rows is NULL in SQLite, and a None here would render as
    # "unranked None" in the board footer rather than as a count.
    conn = store.connect(tmp_path / "s.sqlite")
    assert store.live_counts(conn) == {"live": 0, "rankable": 0, "unranked": 0}


def test_rejection_examples_are_capped_per_reason_and_newest_first(tmp_path):
    """A count says a filter is big; an example says it is wrong.

    'open linked PR' at 562 looked like a busy world until someone opened
    three of them. Newest first by our own clock, because those are the ones
    still checkable against live GitHub.
    """
    conn = store.connect(tmp_path / "s.sqlite")
    for number, seen in ((1, 10.0), (2, 40.0), (3, 20.0), (4, 30.0)):
        store.upsert_candidate(conn, _candidate(number=number),
                               rejected="open linked PR", now=seen)
    store.upsert_candidate(conn, _candidate(number=5), rejected="fork", now=1.0)

    examples = store.rejection_examples(conn, 3)

    assert [row["number"] for row in examples["open linked PR"]] == [2, 4, 3]
    assert [row["number"] for row in examples["fork"]] == [5]


def test_rejection_examples_ignore_live_candidates(tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    store.upsert_candidate(conn, _candidate(number=1), now=1.0)
    assert store.rejection_examples(conn, 3) == {}


def test_the_comment_ceiling_reaches_rows_already_stored(tmp_path):
    """A filter that only judges what it rediscovers is barely a filter.

    Measured 2026-08-10: 36 live candidates carried more than 100 comments when
    the ceiling shipped. free_reject only sees candidates a source hands it this
    sweep, and a months-old bounty may never be rediscovered -- the same reason
    apply_dismissals and apply_payout_rejections exist.
    """
    conn = store.connect(tmp_path / "s.sqlite")
    store.upsert_candidate(conn, replace(_candidate(number=1), comments=2006), now=1.0)
    store.upsert_candidate(conn, replace(_candidate(number=2), comments=100), now=1.0)
    store.upsert_candidate(conn, replace(_candidate(number=3), comments=0), now=1.0)
    store.upsert_candidate(conn, replace(_candidate(number=4), comments=500),
                           rejected="fork", now=1.0)

    changed = store.apply_comment_ceiling(conn, 100)

    assert changed == 1
    rows = {r["number"]: r["rejected"] for r in
            conn.execute("SELECT number, rejected FROM candidates")}
    assert rows[1] == "2006 comments -- too contested"
    assert rows[2] is None          # exactly at the ceiling is kept
    assert rows[3] is None
    assert rows[4] == "fork"        # an existing verdict is not overwritten
