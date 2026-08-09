"""Watching the repos that pay, instead of searching the ones that do not.

Measured 2026-08-09: Algora rewarded 501 issues in ninety days, but across the
ten most-recently-paying repos there were THREE open unassigned bounties. What a
periodic search still finds is what nobody wanted. The money is only reachable
by catching the label as it lands, which is what the Expensify watcher has
always done for one repo -- generalised here to the payer roster.
"""

import pytest

from sniper import store
from sniper.sources.payer_watch import (CACHE_PREFIX, PayerWatchSource,
                                        looks_like_a_bounty_label)


def _conn(tmp_path):
    return store.connect(tmp_path / "t.sqlite")


def _event(event_id, number, label="💎 Bounty"):
    return {"id": event_id, "event": "labeled", "created_at": "2026-08-09T00:00:00Z",
            "label": {"name": label},
            "issue": {"number": number, "title": f"issue {number}", "state": "open",
                      "labels": [{"name": label}]}}


def _issue(number, amount="$500"):
    return {"number": number, "title": f"issue {number}", "state": "open",
            "comments": 1, "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-09T00:00:00Z",
            "html_url": f"https://github.com/pays/well/issues/{number}",
            "assignee": None, "labels": [{"name": "💎 Bounty"}, {"name": amount}]}


@pytest.mark.parametrize("label,expected", [
    ("💎 Bounty", True), ("$780", True), ("$1.2k", True),
    ("bounty", True), ("Bounty: ready", True),
    ("help wanted", False), ("bug", False), ("Stale", False),
])
def test_which_labels_mean_money(label, expected):
    assert looks_like_a_bounty_label(label) is expected


def test_a_new_bounty_label_becomes_a_candidate(tmp_path, fake_get):
    fake_get.queue(200, [_event(1, 42)], {"ETag": 'W/"a"'})
    fake_get.queue(200, _issue(42))
    result = PayerWatchSource(repos=["pays/well"]).discover(_conn(tmp_path))
    assert [c.number for c in result.candidates] == [42]
    assert result.candidates[0].source == "payer-watch"
    assert result.candidates[0].amount_usd == 500
    assert result.warnings == []


def test_an_unchanged_repo_costs_one_request_and_yields_nothing(tmp_path, fake_get):
    """A 304 leaves X-RateLimit-Remaining untouched, measured live.

    That is what makes watching dozens of repos every ten minutes affordable.
    """
    conn = _conn(tmp_path)
    fake_get.queue(200, [], {"ETag": 'W/"a"'})
    PayerWatchSource(repos=["pays/well"]).discover(conn)
    assert store.get_cache(conn, CACHE_PREFIX + "pays/well")[0] == 'W/"a"'
    fake_get.queue(304, None)
    result = PayerWatchSource(repos=["pays/well"]).discover(conn)
    assert result.candidates == []
    assert result.warnings == []
    # The point is not merely that nothing came back -- it is that the request
    # was CONDITIONAL, which is what makes it free.
    assert fake_get.sent_headers[-1]["If-None-Match"] == 'W/"a"'
    # And a 304 must not overwrite the cached ETag with nothing.
    assert store.get_cache(conn, CACHE_PREFIX + "pays/well")[0] == 'W/"a"' 


def test_a_label_that_is_not_money_is_ignored(tmp_path, fake_get):
    fake_get.queue(200, [_event(1, 42, label="Stale")], {"ETag": 'W/"a"'})
    result = PayerWatchSource(repos=["pays/well"]).discover(_conn(tmp_path))
    assert result.candidates == []


def test_one_dead_repo_does_not_end_the_sweep(tmp_path, fake_get):
    """The others are where the money is."""
    fake_get.queue(500, {"message": "boom"})
    fake_get.queue(200, [_event(2, 7)], {"ETag": 'W/"b"'})
    fake_get.queue(200, _issue(7))
    result = PayerWatchSource(repos=["broken/repo", "pays/well"]).discover(_conn(tmp_path))
    assert [c.number for c in result.candidates] == [7]
    assert any("broken/repo" in w for w in result.warnings)


def test_an_already_seen_event_is_not_refetched(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, [_event(1, 42)], {"ETag": 'W/"a"'})
    fake_get.queue(200, _issue(42))
    PayerWatchSource(repos=["pays/well"]).discover(conn)
    calls = len(fake_get.calls)
    fake_get.queue(200, [_event(1, 42)], {"ETag": 'W/"b"'})
    result = PayerWatchSource(repos=["pays/well"]).discover(conn)
    assert result.candidates == []
    assert len(fake_get.calls) == calls + 1  # the poll only; no issue fetch


def test_an_empty_roster_says_so_rather_than_reporting_nothing_found(tmp_path, fake_get):
    result = PayerWatchSource(repos=[]).discover(_conn(tmp_path))
    assert result.candidates == []
    assert any("failure to look" in w for w in result.warnings)
    assert fake_get.calls == []


def test_an_exhausted_core_budget_names_what_went_unwatched(tmp_path, fake_get):
    conn = _conn(tmp_path)
    store.reserve(conn, "core", store.QUOTA_LIMIT["core"])
    result = PayerWatchSource(repos=["a/one", "b/two"]).discover(conn)
    assert result.candidates == []
    assert any("unchecked" in w for w in result.warnings)
    assert fake_get.calls == []
