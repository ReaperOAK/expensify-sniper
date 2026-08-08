"""The candidates table.

bids.issue is an INTEGER PRIMARY KEY -- a single-repo assumption that collides
the moment a second source appears. candidates is keyed on (source, repo,
number) instead. The old tables are left alone so verify.py keeps working.

first_seen_at is OUR freshness clock. It is meaningless on run 1 and accurate
forever after, which is exactly why GitHub's created_at is displayed but never
scored.
"""

from sniper import store
from sniper.models import Candidate


def _conn(tmp_path):
    return store.connect(tmp_path / "t.sqlite")


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
