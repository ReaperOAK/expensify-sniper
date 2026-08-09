"""Tracker settings are configurable without a code change.

The query list in particular is tuned empirically against real digest output --
`💎 Bounty` (563 open) and `bounty` (3825, noisy) are the two verified as of
2026-08-05; the rest is expected to move.
"""

from sniper import config


def test_defaults_match_the_approved_design():
    assert config.AMOUNT_FLOOR_USD == 50
    assert config.DIGEST_TOP_N == 25
    assert config.SEARCH_QUOTA_PER_MIN == 30
    assert config.CORE_QUOTA_PER_HOUR == 5000
    assert config.EXPENSIFY_BOUNTY_USD == 250


def test_paid_labels_default_to_the_one_observed_marker():
    assert config.PAID_LABELS == {"💰 rewarded"}


def test_paid_labels_are_lowercased_for_case_insensitive_matching():
    assert all(label == label.lower() for label in config.PAID_LABELS)


def test_bounty_queries_include_the_verified_label_and_sort_nothing_itself():
    assert any("💎 Bounty" in q for q in config.BOUNTY_QUERIES)
    # sort/order are search_issues() parameters, never baked into the query text.
    assert all("sort:" not in q for q in config.BOUNTY_QUERIES)


def test_every_query_narrows_to_open_issues_at_the_source():
    for query in config.BOUNTY_QUERIES:
        assert "is:issue" in query
        assert "state:open" in query


def test_core_budget_per_run_leaves_headroom_for_the_expensify_watcher():
    assert config.CORE_BUDGET_PER_RUN < config.CORE_QUOTA_PER_HOUR


def test_budget_wait_covers_a_whole_search_window():
    # The autouse no_budget_waiting fixture (tests/conftest.py) zeroes the live
    # config.BUDGET_WAIT_S attribute for every test so the suite never sleeps.
    # That means this test cannot read the configured default off the live
    # module; reload re-derives it fresh from the environment.
    import importlib

    assert importlib.reload(config).BUDGET_WAIT_S > 60


def test_env_overrides_the_amount_floor(monkeypatch):
    monkeypatch.setenv("AMOUNT_FLOOR_USD", "200")
    import importlib

    reloaded = importlib.reload(config)
    try:
        assert reloaded.AMOUNT_FLOOR_USD == 200
    finally:
        monkeypatch.delenv("AMOUNT_FLOOR_USD")
        importlib.reload(config)
