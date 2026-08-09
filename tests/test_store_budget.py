"""The budget guard.

hunt.py and the Expensify watcher share ONE token and can otherwise starve each
other invisibly. A throttled search is indistinguishable from "no results"
(HANDOFF.md trap #3), so the guard refuses to start a run it cannot finish
rather than reporting an empty world.

Search is 30/min; core is 5000/hr. Different windows, so they are tracked
separately and the caller says which it is spending.
"""

import pytest

from sniper import store

MINUTE = 60.0
HOUR = 3600.0


def _conn(tmp_path):
    return store.connect(tmp_path / "t.sqlite")


def test_quota_limits_match_the_measured_api(tmp_path):
    assert store.QUOTA_LIMIT["search"] == 30
    assert store.QUOTA_LIMIT["core"] == 5000
    assert store.WINDOW_SECONDS["search"] == 60
    assert store.WINDOW_SECONDS["core"] == 3600


def test_reserve_succeeds_within_the_limit_and_accumulates(tmp_path):
    conn = _conn(tmp_path)
    assert store.reserve(conn, "search", 10, now=0.0) is True
    assert store.spent(conn, "search", now=0.0) == 10
    assert store.reserve(conn, "search", 20, now=30.0) is True
    assert store.spent(conn, "search", now=30.0) == 30


def test_reserve_refuses_to_exceed_the_limit_and_spends_nothing(tmp_path):
    conn = _conn(tmp_path)
    store.reserve(conn, "search", 30, now=0.0)
    assert store.reserve(conn, "search", 1, now=0.0) is False
    assert store.spent(conn, "search", now=0.0) == 30


def test_the_window_rolls_over(tmp_path):
    conn = _conn(tmp_path)
    store.reserve(conn, "search", 30, now=0.0)
    assert store.reserve(conn, "search", 30, now=MINUTE) is True
    assert store.spent(conn, "search", now=MINUTE) == 30
    assert store.spent(conn, "search", now=0.0) == 30


def test_search_and_core_budgets_are_independent(tmp_path):
    conn = _conn(tmp_path)
    store.reserve(conn, "search", 30, now=0.0)
    assert store.reserve(conn, "core", 4000, now=0.0) is True
    assert store.spent(conn, "search", now=0.0) == 30
    assert store.spent(conn, "core", now=0.0) == 4000


def test_two_connections_share_one_budget(tmp_path):
    """The Expensify watcher and hunt.py are separate processes on one token."""
    a = _conn(tmp_path)
    b = store.connect(tmp_path / "t.sqlite")
    assert store.reserve(a, "search", 25, now=0.0) is True
    assert store.reserve(b, "search", 10, now=0.0) is False
    assert store.reserve(b, "search", 5, now=0.0) is True


def test_reserve_or_wait_sleeps_to_the_next_window_and_retries(tmp_path):
    conn = _conn(tmp_path)
    store.reserve(conn, "search", 30, now=0.0)
    slept = []
    clock = {"t": 10.0}

    def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    assert store.reserve_or_wait(
        conn, "search", 1, max_wait_s=180,
        clock=lambda: clock["t"], sleep=sleep) is True
    assert slept == [50.0]
    assert store.spent(conn, "search", now=clock["t"]) == 1


def test_reserve_or_wait_gives_up_rather_than_sleeping_forever(tmp_path):
    conn = _conn(tmp_path)
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds
        store.reserve(conn, "search", 30, now=clock["t"])  # someone else eats it

    store.reserve(conn, "search", 30, now=0.0)
    assert store.reserve_or_wait(
        conn, "search", 1, max_wait_s=120,
        clock=lambda: clock["t"], sleep=sleep) is False


def test_an_impossible_request_raises_rather_than_looking_like_throttling(tmp_path):
    # A whole search window holds 30. Asking for 31 can NEVER succeed, so it is a
    # caller bug, not congestion. Returning False would render it in the digest as
    # a routine "rate limited" warning forever -- the exact "error looks like
    # absent" trap the budget guard exists to prevent.
    conn = _conn(tmp_path)
    slept = []
    with pytest.raises(ValueError, match="never succeed"):
        store.reserve_or_wait(conn, "search", 31, max_wait_s=180,
                              clock=lambda: 0.0, sleep=slept.append)
    assert slept == []


def test_an_unknown_quota_raises_on_both_entry_points(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="unknown quota"):
        store.reserve(conn, "nonsense", 1, now=0.0)
    with pytest.raises(ValueError, match="unknown quota"):
        store.reserve_or_wait(conn, "nonsense", 1, clock=lambda: 0.0)


def test_max_wait_s_defaults_to_the_configured_budget_wait(tmp_path, monkeypatch):
    """The autouse no_budget_waiting fixture zeroes it, so this must not sleep."""
    from sniper import config

    conn = _conn(tmp_path)
    store.reserve(conn, "search", 30, now=0.0)
    slept = []
    assert config.BUDGET_WAIT_S == 0
    assert store.reserve_or_wait(
        conn, "search", 1, clock=lambda: 0.0, sleep=slept.append) is False
    assert slept == []


def test_prune_budget_drops_stale_windows_but_keeps_the_current_one(tmp_path):
    conn = _conn(tmp_path)
    store.reserve(conn, "search", 5, now=0.0)
    store.reserve(conn, "core", 5, now=0.0)
    store.prune_budget(conn, now=3 * HOUR)
    assert conn.execute("SELECT COUNT(*) FROM rate_budget").fetchone()[0] == 0
    store.reserve(conn, "core", 5, now=3 * HOUR)
    store.prune_budget(conn, now=3 * HOUR)
    assert store.spent(conn, "core", now=3 * HOUR) == 5


def test_a_core_wait_is_capped_by_max_wait_s_not_by_the_window(tmp_path):
    """The core window is 3600s. Sleeping to its boundary ignores max_wait_s.

    The waited>=limit check runs before the sleep is computed, so without a cap
    the FIRST sleep is always 'time to the next boundary' -- up to an hour for
    core, with no signal, contradicting this function's own docstring.
    """
    conn = _conn(tmp_path)
    store.reserve(conn, "core", store.QUOTA_LIMIT["core"], now=0.0)
    slept = []
    clock = {"t": 0.0}

    def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    assert store.reserve_or_wait(
        conn, "core", 1, max_wait_s=65,
        clock=lambda: clock["t"], sleep=sleep) is False
    assert sum(slept) <= 65
    assert max(slept) <= 65
