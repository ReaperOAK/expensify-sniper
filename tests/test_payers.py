"""Payout history: the signal that reorders everything.

Measured 2026-08-09 against live inventory -- ClankerNation/OpenAgents had 201
open Algora bounties, 0 ever rewarded, 0 ever closed, and top-scored the digest
on amount alone. activepieces/activepieces offered $200 and had rewarded 150.
Amount alone ranks them exactly backwards.
"""

import json

from sniper import payers, store

ALGORA = ("💎 Bounty", "$9000")
OWN = ("bounty", "car port")


def _conn(tmp_path):
    return store.connect(tmp_path / "t.sqlite")


def _page(repos):
    return {"total_count": len(repos), "incomplete_results": False,
            "items": [{"number": i, "repository_url":
                       f"https://api.github.com/repos/{r}"}
                      for i, r in enumerate(repos)]}


def test_a_farm_shape_is_rejected():
    reason = payers.payout_reject("ClankerNation/OpenAgents", ALGORA, 201,
                                  {"activepieces/activepieces"})
    assert reason is not None
    assert "0 ever rewarded" in reason


def test_a_proven_payer_is_kept():
    assert payers.payout_reject("activepieces/activepieces", ALGORA, 1,
                                {"activepieces/activepieces"}) is None


def test_a_repo_running_its_own_programme_is_never_touched():
    """commaai/opendbc uses no Algora label and has closed 7 bounties.

    Absence from Algora's rewarded roster says nothing about a repo that does
    not use Algora, so silence must not be read as guilt.
    """
    assert payers.payout_reject("commaai/opendbc", OWN, 2,
                                {"activepieces/activepieces"}) is None


def test_a_small_new_repo_is_given_the_benefit_of_the_doubt():
    assert payers.payout_reject("someone/newproject", ALGORA, 2,
                                {"activepieces/activepieces"}) is None


def test_an_unavailable_roster_abstains_rather_than_rejecting_the_world():
    # An empty roster means we could not look, not that nobody pays.
    assert payers.payout_reject("ClankerNation/OpenAgents", ALGORA, 201, set()) is None


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
