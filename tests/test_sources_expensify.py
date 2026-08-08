"""Expensify as one source among several.

Help Wanted issues pay a flat rate, so there is no $-label to parse; the amount
is a config constant. The event poll is ETag-cached because a 304 costs zero
core quota, and mark_event_seen() gives the same "new to us" semantics the
candidates table gives the other sources.
"""

from sniper import store
from sniper.sources.expensify import ExpensifySource


def _conn(tmp_path):
    return store.connect(tmp_path / "t.sqlite")


def _event(event_id, number, label="Help Wanted"):
    return {
        "id": event_id, "event": "labeled", "created_at": "2026-08-01T00:00:00Z",
        "label": {"name": label},
        "issue": {"number": number, "title": f"issue {number}", "state": "open",
                  "labels": [{"name": label}]},
    }


def _issue(number, comments=3):
    return {
        "number": number, "title": f"issue {number}", "state": "open",
        "comments": comments, "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "html_url": f"https://github.com/Expensify/App/issues/{number}",
        "assignee": None, "labels": [{"name": "Help Wanted"}],
    }


def test_new_help_wanted_event_becomes_a_candidate_at_the_flat_rate(tmp_path, fake_get):
    from sniper import config

    fake_get.queue(200, [_event(1, 96348)], {"ETag": 'W/"a"'})
    fake_get.queue(200, _issue(96348))
    result = ExpensifySource().discover(_conn(tmp_path))
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert cand.source == "expensify"
    assert cand.repo == "Expensify/App"
    assert cand.number == 96348
    assert cand.amount_usd == config.EXPENSIFY_BOUNTY_USD
    assert cand.amounts_seen == ()
    assert result.warnings == []


def test_the_etag_is_stored_and_replayed(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, [], {"ETag": 'W/"a"'})
    ExpensifySource().discover(conn)
    assert store.get_cache(conn, ExpensifySource.CACHE_KEY)[0] == 'W/"a"'
    fake_get.queue(304, None)
    result = ExpensifySource().discover(conn)
    assert result.candidates == []
    assert fake_get.calls[-1][0].endswith("/repos/Expensify/App/issues/events")


def test_an_already_seen_event_does_not_cost_an_issue_fetch(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, [_event(1, 96348)], {"ETag": 'W/"a"'})
    fake_get.queue(200, _issue(96348))
    ExpensifySource().discover(conn)
    calls_after_first = len(fake_get.calls)
    fake_get.queue(200, [_event(1, 96348)], {"ETag": 'W/"b"'})
    result = ExpensifySource().discover(conn)
    assert result.candidates == []
    assert len(fake_get.calls) == calls_after_first + 1  # only the poll


def test_a_failed_poll_becomes_a_warning_not_an_empty_success(tmp_path, fake_get):
    fake_get.queue(403, {"message": "API rate limit exceeded"},
                   {"X-RateLimit-Reset": "1"})
    result = ExpensifySource().discover(_conn(tmp_path))
    assert result.candidates == []
    assert any("rate limited" in w for w in result.warnings)


def test_a_failed_issue_fetch_warns_and_the_other_events_still_land(tmp_path, fake_get):
    fake_get.queue(200, [_event(1, 1), _event(2, 2)], {"ETag": 'W/"a"'})
    fake_get.queue(500, {"message": "boom"})
    fake_get.queue(200, _issue(2))
    result = ExpensifySource().discover(_conn(tmp_path))
    assert [c.number for c in result.candidates] == [2]
    assert any("#1" in w for w in result.warnings)


def test_an_exhausted_core_budget_warns_and_makes_no_call(tmp_path, fake_get):
    conn = _conn(tmp_path)
    store.reserve(conn, "core", 5000)
    result = ExpensifySource().discover(conn)
    assert result.candidates == []
    assert any("core budget" in w for w in result.warnings)
    assert fake_get.calls == []


def test_a_fetch_failure_leaves_the_event_unseen_so_it_is_retried(tmp_path, fake_get):
    """Marking before the work burns the event forever.

    mark_event_seen() commits, so an event marked before a failed fetch is
    skipped as "already handled" on every later run. A warning would make that
    visible but not recoverable -- a $250 bounty silently dropped.
    """
    conn = _conn(tmp_path)
    fake_get.queue(200, [_event(1, 96348)], {"ETag": 'W/"a"'})
    fake_get.queue(500, {"message": "boom"})
    first = ExpensifySource().discover(conn)
    assert first.candidates == []
    assert any("96348" in w for w in first.warnings)
    assert store.event_seen(conn, 1) is False

    # Next run: same event, this time the fetch works.
    fake_get.queue(200, [_event(1, 96348)], {"ETag": 'W/"b"'})
    fake_get.queue(200, _issue(96348))
    second = ExpensifySource().discover(conn)
    assert [c.number for c in second.candidates] == [96348]
    assert store.event_seen(conn, 1) is True


def test_budget_exhausted_mid_loop_leaves_the_event_unseen(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, [_event(1, 1), _event(2, 2)], {"ETag": 'W/"a"'})
    fake_get.queue(200, _issue(1))
    # One core unit for the poll, one for issue #1 -- then nothing left for #2.
    store.reserve(conn, "core", store.QUOTA_LIMIT["core"] - 2)
    result = ExpensifySource().discover(conn)
    assert [c.number for c in result.candidates] == [1]
    assert any("core budget" in w and "retried next run" in w for w in result.warnings)
    assert store.event_seen(conn, 1) is True
    assert store.event_seen(conn, 2) is False


def test_a_malformed_issue_payload_warns_without_discarding_the_others(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, [_event(1, 1), _event(2, 2)], {"ETag": 'W/"a"'})
    fake_get.queue(200, {"title": "no number key"})  # Candidate.from_issue will raise
    fake_get.queue(200, _issue(2))
    result = ExpensifySource().discover(conn)
    assert [c.number for c in result.candidates] == [2]
    assert any("unusable" in w for w in result.warnings)
    assert store.event_seen(conn, 1) is False
