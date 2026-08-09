"""Payout history: the signal that reorders everything.

Measured 2026-08-09 against live inventory -- ClankerNation/OpenAgents had 201
open Algora bounties, 0 ever rewarded, 0 ever closed, and top-scored the digest
on amount alone. activepieces/activepieces offered $200 and had rewarded 150.
Amount alone ranks them exactly backwards.
"""

import json

from sniper import config, payers, store

ALGORA = ("💎 Bounty", "$9000")
# Under the OLD rule this represented "some label that is not Algora's exact
# string" -- but "bounty" is now caught by bounty_label_of's substring match,
# so it no longer demonstrates an abstain-on-no-label case. Nothing in this
# tuple marks the repo as running a bounty programme at all.
NO_BOUNTY_LABEL = ("bug", "car port")


def _conn(tmp_path):
    return store.connect(tmp_path / "t.sqlite")


def _page(repos):
    return {"total_count": len(repos), "incomplete_results": False,
            "items": [{"number": i, "repository_url":
                       f"https://api.github.com/repos/{r}"}
                      for i, r in enumerate(repos)]}


def test_a_farm_shape_is_rejected():
    reason = payers.payout_reject("ClankerNation/OpenAgents", ALGORA, 201,
                                  {"activepieces/activepieces"}, closed_count=0)
    assert reason is not None
    assert "0 ever closed" in reason


def test_a_proven_payer_is_kept():
    assert payers.payout_reject("activepieces/activepieces", ALGORA, 1,
                                {"activepieces/activepieces"},
                                closed_count=173) is None


def test_a_repo_with_no_bounty_ish_label_is_never_touched():
    """Nothing in this repo's labels marks it as running a bounty programme.

    Under the OLD rule this abstained because the repo did not wear Algora's
    exact "💎 Bounty" string. Under the NEW rule, plain "bounty" is caught too
    -- so the abstain condition that actually survives is "no label here marks
    this as a bounty at all", and that is what this now tests: a repo's silence
    about a label it does not use says nothing about whether it pays.
    """
    assert payers.payout_reject("commaai/opendbc", NO_BOUNTY_LABEL, 20,
                                {"activepieces/activepieces"},
                                closed_count=0) is None


def test_a_small_new_repo_is_given_the_benefit_of_the_doubt():
    assert payers.payout_reject("someone/newproject", ALGORA, 2,
                                {"activepieces/activepieces"},
                                closed_count=0) is None


def test_an_unavailable_roster_abstains_rather_than_rejecting_the_world():
    # An empty roster means we could not look, not that nobody pays.
    assert payers.payout_reject("ClankerNation/OpenAgents", ALGORA, 201, set(),
                                closed_count=0) is None


def test_known_payers_collects_distinct_repos(tmp_path, fake_get):
    fake_get.queue(200, _page(["a/one", "b/two", "a/one"]))
    repos, warnings = payers.known_payers(_conn(tmp_path), max_pages=1, now=1000.0)
    assert repos == {"a/one", "b/two"}
    assert warnings == []


def test_the_roster_is_cached_so_a_second_run_makes_no_request(tmp_path, fake_get):
    conn = _conn(tmp_path)
    fake_get.queue(200, _page(["a/one"]))
    payers.known_payers(conn, max_pages=1, now=1000.0)
    calls = len(fake_get.calls)
    repos, _ = payers.known_payers(conn, max_pages=1, now=1000.0 + 60)
    assert repos == {"a/one"}
    assert len(fake_get.calls) == calls  # nothing new was fetched


def test_a_stale_cache_is_refetched(tmp_path, fake_get):
    from sniper import config

    conn = _conn(tmp_path)
    fake_get.queue(200, _page(["a/one"]))
    payers.known_payers(conn, max_pages=1, now=1000.0)
    fake_get.queue(200, _page(["a/one", "c/three"]))
    repos, _ = payers.known_payers(
        conn, max_pages=1, now=1000.0 + config.PAYER_CACHE_S + 1)
    assert repos == {"a/one", "c/three"}


def test_a_failed_lookup_warns_and_abstains(tmp_path, fake_get):
    fake_get.queue(403, {"message": "API rate limit exceeded"},
                   {"X-RateLimit-Reset": "1"})
    repos, warnings = payers.known_payers(_conn(tmp_path), max_pages=1, now=1000.0)
    assert repos == set()
    assert any("payout history" in w for w in warnings)


def test_a_partial_roster_says_so(tmp_path, fake_get):
    page = _page(["a/one"])
    page["incomplete_results"] = True
    page["total_count"] = 900
    fake_get.queue(200, page)
    _, warnings = payers.known_payers(_conn(tmp_path), max_pages=1, now=1000.0)
    assert any("partial" in w for w in warnings)


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
