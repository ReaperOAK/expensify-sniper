"""Discovery over GitHub bounty labels.

Sorted by `updated`, never `created`: bounty labels land on issues that already
exist, so `created` ordering misses essentially the whole inventory. "New to us"
means absent from the candidates table, or present with a different updated_at.

Every failure mode becomes a warning string rather than a shorter list. A source
that quietly returns fewer results is the exact shape of HANDOFF.md trap #3.
"""

import pytest

from sniper import github_api, store
from sniper.sources.bounty_labels import BountyLabelSource
from tests.conftest import load_fixture


def _conn(tmp_path):
    return store.connect(tmp_path / "t.sqlite")


def _page(numbers, total=None, incomplete=False, updated="2026-08-01T00:00:00Z"):
    return {
        "total_count": total if total is not None else len(numbers),
        "incomplete_results": incomplete,
        "items": [{
            "number": n, "title": f"issue {n}", "state": "open", "comments": 0,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": updated,
            "html_url": f"https://github.com/acme/widgets/issues/{n}",
            "repository_url": "https://api.github.com/repos/acme/widgets",
            "assignee": None, "labels": [{"name": "$780"}],
        } for n in numbers],
    }


def test_discovers_candidates_from_one_query(tmp_path, fake_get):
    fake_get.queue(200, _page([1, 2, 3]))
    result = BountyLabelSource(queries=["label:bounty"]).discover(_conn(tmp_path))
    assert [c.number for c in result.candidates] == [1, 2, 3]
    assert all(c.source == "github-labels" for c in result.candidates)
    assert result.warnings == []


def test_sorts_by_updated_not_created(tmp_path, fake_get):
    fake_get.queue(200, _page([1]))
    BountyLabelSource(queries=["label:bounty"]).discover(_conn(tmp_path))
    _, params = fake_get.calls[0]
    assert params["sort"] == "updated"
    assert params["order"] == "desc"


def test_runs_every_configured_query(tmp_path, fake_get):
    fake_get.queue(200, _page([1]))
    fake_get.queue(200, _page([2]))
    # max_pages=2: BOUNTY_SEARCH_MAX_PAGES is 20, and two queries reserving 20
    # each exceed the 30/min search window. That is real, and the budget guard
    # is tested separately -- this test is about running every query.
    result = BountyLabelSource(queries=["a", "b"], max_pages=2).discover(_conn(tmp_path))
    assert [q for _, p in fake_get.calls for q in [p["q"]]] == ["a", "b"]
    assert {c.number for c in result.candidates} == {1, 2}


def test_deduplicates_a_candidate_matched_by_two_queries(tmp_path, fake_get):
    fake_get.queue(200, _page([1, 2]))
    fake_get.queue(200, _page([2, 3]))
    result = BountyLabelSource(queries=["a", "b"], max_pages=2).discover(_conn(tmp_path))
    assert sorted(c.number for c in result.candidates) == [1, 2, 3]


def test_incomplete_results_becomes_a_warning_not_a_silent_short_list(tmp_path, fake_get):
    fake_get.queue(200, _page([1], incomplete=True))
    result = BountyLabelSource(queries=["label:bounty"]).discover(_conn(tmp_path))
    assert len(result.candidates) == 1
    assert any("incomplete" in w for w in result.warnings)
    assert any("label:bounty" in w for w in result.warnings)


def test_a_throttled_query_warns_and_the_other_queries_still_run(tmp_path, fake_get):
    fake_get.queue(403, {"message": "API rate limit exceeded"},
                   {"X-RateLimit-Reset": "1"})
    fake_get.queue(200, _page([9]))
    result = BountyLabelSource(queries=["a", "b"], max_pages=2).discover(_conn(tmp_path))
    assert [c.number for c in result.candidates] == [9]
    assert any("rate limited" in w and "'a'" in w for w in result.warnings)


def test_an_exhausted_search_budget_warns_and_spends_nothing(tmp_path, fake_get):
    conn = _conn(tmp_path)
    store.reserve(conn, "search", 30)
    result = BountyLabelSource(queries=["label:bounty"], max_pages=10).discover(conn)
    assert result.candidates == []
    assert any("search budget" in w for w in result.warnings)
    assert fake_get.calls == []


def test_stops_paging_once_a_whole_page_is_already_known(tmp_path, fake_get):
    from sniper.models import Candidate

    conn = _conn(tmp_path)
    # total=200 (not the default 100): with the default, search_issues' own
    # `len(items) >= total_count` guard ends paging after page 1 no matter what
    # the `stop` callback says, and this test passes without ever exercising the
    # incremental-discovery mechanism it exists to cover. Verified by mutation:
    # with total=100 the test still passes when `stop` is replaced by None.
    known = _page(list(range(100)), total=200)
    for item in known["items"]:
        store.upsert_candidate(conn, Candidate.from_search_item("github-labels", item))
    fake_get.queue(200, known)
    fake_get.queue(200, _page(list(range(100, 200)), total=200))
    result = BountyLabelSource(queries=["label:bounty"]).discover(conn)
    assert len(fake_get.calls) == 1
    assert len(result.candidates) == 100


def test_a_known_issue_with_a_new_updated_at_does_not_stop_paging(tmp_path, fake_get):
    from sniper.models import Candidate

    conn = _conn(tmp_path)
    stale = _page(list(range(100)), updated="2026-07-01T00:00:00Z")
    for item in stale["items"]:
        store.upsert_candidate(conn, Candidate.from_search_item("github-labels", item))
    # total=101 (not the default 100): otherwise search_issues' own
    # `len(items) >= total_count` guard ends paging after page 1 regardless of
    # what the `stop` callback under test says, and this test would pass
    # vacuously without ever exercising it.
    fake_get.queue(200, _page(list(range(100)), total=101, updated="2026-08-04T00:00:00Z"))
    fake_get.queue(200, _page([200]))
    BountyLabelSource(queries=["label:bounty"]).discover(conn)
    assert len(fake_get.calls) == 2


def test_the_real_recorded_payload_yields_candidates(tmp_path, fake_get):
    # The recorded fixture is genuinely page 1 of a >100-result query
    # (total_count=520): only one page was ever recorded, so max_pages=1 here
    # bounds the fetch to what is actually queued. This test is about whether
    # real payload shape parses cleanly into Candidates, not about pagination.
    fake_get.queue(200, load_fixture("search_bounty_page1"))
    result = BountyLabelSource(queries=["label:bounty"], max_pages=1).discover(_conn(tmp_path))
    assert len(result.candidates) == 100
    assert result.warnings == []


def test_a_malformed_search_item_warns_without_discarding_the_rest(tmp_path, fake_get):
    """Only the network call used to be guarded.

    A result item missing repository_url raised out of discover() and took every
    candidate already collected with it -- silently, which is the exact failure
    the warnings-as-value contract exists to prevent.
    """
    page = _page([1, 2])
    del page["items"][0]["repository_url"]
    fake_get.queue(200, page)
    result = BountyLabelSource(queries=["label:bounty"]).discover(_conn(tmp_path))
    assert [c.number for c in result.candidates] == [2]
    assert any("unusable result item" in w for w in result.warnings)
