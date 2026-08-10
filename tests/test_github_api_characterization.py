"""Behavior of sniper.github_api pinned BEFORE the repo-agnostic refactor.

These assertions are the contract. They must pass identically before and after
Task 4. If one of them changes, the refactor changed behavior -- which it is
not allowed to do.

Every test drives a scripted requests.get; nothing here touches the network.
"""

import pytest

from sniper import github_api
from tests.conftest import load_fixture


def test_headers_carry_token_accept_and_api_version():
    headers = github_api._headers()
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert headers["Authorization"].startswith("Bearer ")
    assert headers["User-Agent"] == github_api.UA


def test_headers_merge_extra_without_dropping_defaults():
    headers = github_api._headers({"If-None-Match": 'W/"abc"'})
    assert headers["If-None-Match"] == 'W/"abc"'
    assert "Authorization" in headers


def test_get_raises_rate_limited_on_403_mentioning_rate_limit(fake_get):
    fake_get.queue(403, {"message": "API rate limit exceeded"},
                   {"X-RateLimit-Reset": "1234"})
    with pytest.raises(github_api.RateLimited) as excinfo:
        github_api._get("/rate_limit")
    assert "1234" in str(excinfo.value)


def test_get_does_not_raise_on_403_that_is_not_a_rate_limit(fake_get):
    fake_get.queue(403, {"message": "Resource not accessible"})
    resp = github_api._get("/anything")
    assert resp.status_code == 403


def test_poll_returns_unchanged_and_costs_nothing_on_304(fake_get):
    fake_get.queue(304, None)
    events, etag, changed, newest = github_api.poll_help_wanted_events(
        "owner/repo", etag='W/"old"')
    assert events == []
    assert etag == 'W/"old"'
    assert changed is False
    assert newest is None


def test_poll_keeps_only_help_wanted_label_events_on_issues(fake_get):
    payload = [
        {"id": 1, "event": "labeled", "created_at": "2026-08-01T00:00:00Z",
         "label": {"name": "Help Wanted"},
         "issue": {"number": 10, "title": "keep me", "state": "open",
                   "labels": [{"name": "Help Wanted"}, {"name": "Bug"}]}},
        {"id": 2, "event": "labeled", "created_at": "2026-08-01T00:01:00Z",
         "label": {"name": "Reviewing"},
         "issue": {"number": 11, "title": "wrong label", "state": "open", "labels": []}},
        {"id": 3, "event": "closed", "created_at": "2026-08-01T00:02:00Z",
         "issue": {"number": 12, "title": "wrong event", "state": "closed", "labels": []}},
        {"id": 4, "event": "labeled", "created_at": "2026-08-01T00:03:00Z",
         "label": {"name": "Help Wanted"},
         "issue": {"number": 13, "title": "a pull request", "state": "open",
                   "labels": [], "pull_request": {"url": "x"}}},
    ]
    fake_get.queue(200, payload, {"ETag": 'W/"new"'})
    events, etag, changed, _ = github_api.poll_help_wanted_events(
        "owner/repo", etag=None)
    assert changed is True
    assert etag == 'W/"new"'
    assert events == [{
        "event_id": 1,
        "issue": 10,
        "title": "keep me",
        "state": "open",
        "labeled_at": "2026-08-01T00:00:00Z",
        "labels": ["Help Wanted", "Bug"],
    }]


def test_poll_survives_the_real_recorded_events_payload(fake_get):
    fake_get.queue(200, load_fixture("events_expensify"), {"ETag": 'W/"real"'})
    events, etag, changed, _ = github_api.poll_help_wanted_events(
        "Expensify/App", etag=None)
    assert changed is True
    assert etag == 'W/"real"'
    assert fake_get.calls[0][0] == "https://api.github.com/repos/Expensify/App/issues/events"
    assert all(set(e) == {"event_id", "issue", "title", "state", "labeled_at", "labels"}
               for e in events)
    assert all(isinstance(e["issue"], int) for e in events)


def test_iter_comments_stops_on_a_short_page(fake_get):
    fake_get.queue(200, [{"id": 1}, {"id": 2}])
    assert [c["id"] for c in github_api.iter_comments("owner/repo", 1)] == [1, 2]
    assert len(fake_get.calls) == 1


def test_iter_comments_pages_while_pages_are_full(fake_get):
    fake_get.queue(200, [{"id": i} for i in range(100)])
    fake_get.queue(200, [{"id": 100}])
    ids = [c["id"] for c in github_api.iter_comments("owner/repo", 1)]
    assert ids == list(range(101))
    assert [params["page"] for _, params in fake_get.calls] == [1, 2]


def test_existing_proposals_skips_bots_and_non_proposals_and_sorts_oldest_first(fake_get):
    proposal = (
        "Proposal\n"
        "### What is the root cause of that problem?\ncause\n"
        "### What changes do you think we should make in order to solve the problem?\nfix\n"
    )
    fake_get.queue(200, [
        {"id": 1, "body": proposal, "created_at": "2026-08-02T00:00:00Z",
         "html_url": "u2", "user": {"login": "second", "type": "User"}},
        {"id": 2, "body": proposal, "created_at": "2026-08-01T00:00:00Z",
         "html_url": "u1", "user": {"login": "first", "type": "User"}},
        {"id": 3, "body": proposal, "created_at": "2026-08-01T00:00:00Z",
         "html_url": "ub", "user": {"login": "MelvinBot", "type": "Bot"}},
        {"id": 4, "body": "just a comment", "created_at": "2026-08-01T00:00:00Z",
         "html_url": "uc", "user": {"login": "third", "type": "User"}},
    ])
    got = github_api.existing_proposals("owner/repo", 1)
    assert [p["author"] for p in got] == ["first", "second"]
    assert set(got[0]) == {"author", "created_at", "url", "body"}


def test_melvin_analysis_returns_the_root_cause_comment(fake_get):
    fake_get.queue(200, [
        {"id": 1, "body": "hello", "user": {"login": "someone"}},
        {"id": 2, "body": "The ROOT CAUSE is X", "user": {"login": "melvin-bot[bot]"}},
    ])
    assert github_api.melvin_analysis("owner/repo", 1) == "The ROOT CAUSE is X"


def test_melvin_analysis_returns_none_when_absent(fake_get):
    fake_get.queue(200, [{"id": 1, "body": "hello", "user": {"login": "someone"}}])
    assert github_api.melvin_analysis("owner/repo", 1) is None


def test_rate_limit_returns_the_core_resource(fake_get):
    fake_get.queue(200, {"resources": {"core": {"limit": 5000, "remaining": 4999},
                                       "search": {"limit": 30}}})
    assert github_api.rate_limit() == {"limit": 5000, "remaining": 4999}


def test_every_endpoint_uses_the_repo_it_was_given(fake_get):
    fake_get.queue(304, None)
    github_api.poll_help_wanted_events("acme/widgets", etag='W/"x"')
    fake_get.queue(200, {"number": 7})
    github_api.get_issue("acme/widgets", 7)
    fake_get.queue(200, [])
    list(github_api.iter_comments("acme/widgets", 7))
    urls = [url for url, _ in fake_get.calls]
    assert urls == [
        "https://api.github.com/repos/acme/widgets/issues/events",
        "https://api.github.com/repos/acme/widgets/issues/7",
        "https://api.github.com/repos/acme/widgets/issues/7/comments",
    ]


# --- the watermark, and why it has to come from the PAGE ---------------------


def _labeled(event_id, *, label="Help Wanted", number=None):
    return {"id": event_id, "event": "labeled", "created_at": "2026-08-01T00:00:00Z",
            "label": {"name": label},
            "issue": {"number": number or event_id, "title": "t", "state": "open",
                      "labels": []}}


def _full_page(newest_id, *, label="Reviewing"):
    """100 events, ids descending -- the shape that makes paging continue."""
    return [_labeled(newest_id - i, label=label) for i in range(100)]


def test_the_watermark_is_the_newest_id_on_the_page_not_the_newest_match(fake_get):
    """`labeled` + the right label + not-a-PR is a tiny minority of this feed.

    Taking the watermark from the FILTERED list meant it advanced only when
    another matching label appeared, so between matches every poll re-paged the
    full EVENT_MAX_PAGES -- and before the first match it was never written at
    all, leaving since_event_id None and the paging block dead.
    """
    fake_get.queue(200, [_labeled(900, label="Reviewing"), _labeled(880)],
                   {"ETag": 'W/"n"'})
    events, _, _, newest = github_api.poll_help_wanted_events("owner/repo", etag=None)
    assert [e["event_id"] for e in events] == [880]
    assert newest == 900


def test_a_page_with_no_matching_event_still_reports_a_watermark(fake_get):
    """The deployed state the deep-paging feature was added for: Expensify/App
    with no Help Wanted label since the tracker started watching.
    """
    fake_get.queue(200, [_labeled(900, label="Reviewing")], {"ETag": 'W/"n"'})
    events, _, changed, newest = github_api.poll_help_wanted_events(
        "owner/repo", etag=None)
    assert events == []
    assert changed is True
    assert newest == 900


def test_an_empty_page_reports_no_watermark_rather_than_zero(fake_get):
    """None is "the page told us nothing"; 0 would be a real id that stops
    every future page from being read.
    """
    fake_get.queue(200, [], {"ETag": 'W/"n"'})
    _, _, _, newest = github_api.poll_help_wanted_events("owner/repo", etag=None)
    assert newest is None


def test_a_304_reports_no_watermark(fake_get):
    fake_get.queue(304, None)
    assert github_api.poll_help_wanted_events("owner/repo", etag='W/"old"') == \
        ([], 'W/"old"', False, None)


def test_paging_stops_at_the_page_that_reaches_the_watermark(fake_get):
    fake_get.queue(200, _full_page(1000), {"ETag": 'W/"n"'})   # 1000..901
    fake_get.queue(200, _full_page(900))                        # 900..801, past 850
    _, _, _, newest = github_api.poll_help_wanted_events(
        "owner/repo", etag=None, since_event_id=850)
    assert [params.get("page") for _, params in fake_get.calls] == [None, 2]
    assert newest == 1000


def test_paging_stops_at_the_page_cap_when_the_watermark_is_never_reached(fake_get):
    """The first run has no stopping point, which is all the cap is for."""
    from sniper import config

    for page in range(config.EVENT_MAX_PAGES):
        fake_get.queue(200, _full_page(1000 - 100 * page),
                       {"ETag": 'W/"n"'} if page == 0 else None)
    _, _, _, newest = github_api.poll_help_wanted_events(
        "owner/repo", etag=None, since_event_id=1)
    assert len(fake_get.calls) == config.EVENT_MAX_PAGES
    assert newest == 1000


def test_without_a_watermark_only_one_page_is_ever_read(fake_get):
    fake_get.queue(200, _full_page(1000), {"ETag": 'W/"n"'})
    github_api.poll_help_wanted_events("owner/repo", etag=None)
    assert len(fake_get.calls) == 1
