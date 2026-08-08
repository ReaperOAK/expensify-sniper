"""search_issues(): pagination, the 1000-result cap, and honest incompleteness.

A rate-limited or timed-out search must never look like "no results"
(HANDOFF.md trap #3). SearchResult.incomplete carries that distinction, and a
throttle raises RateLimited rather than returning an empty list.
"""

import pytest

from sniper import github_api
from tests.conftest import load_fixture


def _page(n_items, total, incomplete=False, start=0):
    return {
        "total_count": total,
        "incomplete_results": incomplete,
        "items": [{"number": start + i, "title": f"issue {start + i}"} for i in range(n_items)],
    }


def test_single_short_page_makes_one_call(fake_get):
    fake_get.queue(200, _page(3, 3))
    result = github_api.search_issues("label:bounty")
    assert [i["number"] for i in result.items] == [0, 1, 2]
    assert result.total_count == 3
    assert result.incomplete is False
    assert result.pages_fetched == 1
    assert len(fake_get.calls) == 1


def test_query_and_sort_are_sent_as_params(fake_get):
    fake_get.queue(200, _page(1, 1))
    github_api.search_issues('label:"💎 Bounty"', sort="updated", order="desc")
    url, params = fake_get.calls[0]
    assert url == "https://api.github.com/search/issues"
    assert params["q"] == 'label:"💎 Bounty"'
    assert params["sort"] == "updated"
    assert params["order"] == "desc"
    assert params["per_page"] == 100
    assert params["page"] == 1


def test_pages_until_a_short_page(fake_get):
    fake_get.queue(200, _page(100, 150, start=0))
    fake_get.queue(200, _page(50, 150, start=100))
    result = github_api.search_issues("q", per_page=100)
    assert len(result.items) == 150
    assert result.pages_fetched == 2
    assert [p["page"] for _, p in fake_get.calls] == [1, 2]


def test_stops_at_the_1000_result_cap(fake_get):
    for page in range(10):
        fake_get.queue(200, _page(100, 5000, start=page * 100))
    result = github_api.search_issues("q", per_page=100, max_pages=99)
    assert len(result.items) == github_api.SEARCH_RESULT_CAP
    assert result.pages_fetched == 10
    assert result.total_count == 5000


def test_max_pages_bounds_the_search_quota_spend(fake_get):
    fake_get.queue(200, _page(100, 5000, start=0))
    fake_get.queue(200, _page(100, 5000, start=100))
    result = github_api.search_issues("q", per_page=100, max_pages=2)
    assert result.pages_fetched == 2
    assert len(result.items) == 200


def test_incomplete_results_is_surfaced_not_swallowed(fake_get):
    fake_get.queue(200, _page(3, 3, incomplete=True))
    result = github_api.search_issues("q")
    assert result.incomplete is True
    assert len(result.items) == 3


def test_incomplete_on_any_page_marks_the_whole_result(fake_get):
    fake_get.queue(200, _page(100, 150, start=0))
    fake_get.queue(200, _page(50, 150, incomplete=True, start=100))
    assert github_api.search_issues("q").incomplete is True


def test_a_throttled_search_raises_rather_than_returning_empty(fake_get):
    fake_get.queue(403, {"message": "API rate limit exceeded for user"},
                   {"X-RateLimit-Reset": "999"})
    with pytest.raises(github_api.RateLimited):
        github_api.search_issues("q")


def test_stop_callback_halts_paging_early(fake_get):
    fake_get.queue(200, _page(100, 500, start=0))
    fake_get.queue(200, _page(100, 500, start=100))
    seen = []

    def stop(page_items):
        seen.append(len(page_items))
        return len(seen) == 2

    result = github_api.search_issues("q", stop=stop)
    assert result.pages_fetched == 2
    assert seen == [100, 100]


def test_real_recorded_payload_parses(fake_get):
    # Recorded live: 100 items, total_count 520 -- a genuine full first page, not a
    # terminal short one. max_pages=1 keeps this test about payload-shape parsing
    # (pagination mechanics are covered by the tests above) and matches the single
    # fixture queued here, since the strict FIFO fake_get has no second page to give.
    fake_get.queue(200, load_fixture("search_bounty_page1"))
    result = github_api.search_issues(
        'label:"💎 Bounty" state:open is:issue no:assignee -label:"💰 Rewarded"',
        max_pages=1)
    assert result.total_count > 0
    assert result.incomplete is False
    assert len(result.items) == 100
    item = result.items[0]
    assert item["repository_url"].startswith("https://api.github.com/repos/")
    # Corrects the spec: search items carry no repo metadata, so fork/archived
    # are NOT free rejects. Plan 2's pipeline depends on this.
    assert "repository" not in item
