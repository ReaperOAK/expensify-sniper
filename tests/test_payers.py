"""Payout history: the signal that reorders everything.

Measured 2026-08-09 against live inventory -- ClankerNation/OpenAgents had 201
open Algora bounties, 0 ever rewarded, 0 ever closed, and top-scored the digest
on amount alone. activepieces/activepieces offered $200 and had rewarded 150.
Amount alone ranks them exactly backwards.

Closing is not paying, though: measured 2026-08-10, zhangjiayang6835-cyber/
bounty-plaza had grown 198 closed issues under its own "Bounty" label -- enough
to abstain the old ">0 ever closed" rule -- but only 23 of those carried any
linked pull request (12%). Sampling #12, #13 and #16 found them closed
2026-07-12T10:43:51Z / :55Z / :58Z, seven seconds apart, none assigned, one
titled "[Bounty] [bounty][funding-pending]: complete the first permi...". A
bulk close. `ClosedBounties` and the ratio floor in `payout_reject` exist to
tell that apart from activepieces' 170/173 (98%) and commaai/opendbc's 4/7
(57%, no Algora label at all).
"""

import json

from sniper import config, payers, store
from sniper.payers import ClosedBounties

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


def _closed_page(total_count: int):
    return {"total_count": total_count, "incomplete_results": False, "items": []}


def test_a_farm_shape_is_rejected():
    reason = payers.payout_reject("ClankerNation/OpenAgents", ALGORA, 201,
                                  {"activepieces/activepieces"},
                                  closed=ClosedBounties(0, 0))
    assert reason is not None
    assert "0 ever closed" in reason


def test_a_proven_payer_is_kept():
    assert payers.payout_reject("activepieces/activepieces", ALGORA, 1,
                                {"activepieces/activepieces"},
                                closed=ClosedBounties(173, 170)) is None


def test_a_repo_with_no_bounty_ish_label_is_never_touched():
    """Nothing in this repo's labels marks it as running a bounty programme.

    Under the OLD rule this abstained because the repo did not wear Algora's
    exact "💎 Bounty" string. Under the NEW rule, plain "bounty" is caught too
    -- so the abstain condition that actually survives is "no label here marks
    this as a bounty at all", and that is what this now tests: a repo's silence
    about a label it does not use says nothing about whether it pays.
    """
    assert payers.payout_reject("some/other-repo", NO_BOUNTY_LABEL, 20,
                                {"activepieces/activepieces"},
                                closed=ClosedBounties(0, 0)) is None


def test_a_small_new_repo_is_given_the_benefit_of_the_doubt():
    assert payers.payout_reject("someone/newproject", ALGORA, 2,
                                {"activepieces/activepieces"},
                                closed=ClosedBounties(0, 0)) is None


def test_an_unavailable_roster_abstains_rather_than_rejecting_the_world():
    # An empty roster means we could not look, not that nobody pays.
    assert payers.payout_reject("ClankerNation/OpenAgents", ALGORA, 201, set(),
                                closed=ClosedBounties(0, 0)) is None


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


# --- ever_closed_with: two searches, one cache entry ------------------------


def test_counts_closed_issues_and_closed_issues_with_a_linked_pr(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _closed_page(173))
    fake_get.queue(200, _closed_page(170))
    assert (payers.ever_closed_with(conn, "acme/widgets", "💎 Bounty", now=100.0)
            == ClosedBounties(173, 170))


def test_a_farm_reports_zero_of_zero(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _closed_page(0))
    fake_get.queue(200, _closed_page(0))
    assert (payers.ever_closed_with(conn, "farm/repo", "Bounty", now=100.0)
            == ClosedBounties(0, 0))


def test_a_farm_that_bulk_closes_reports_a_low_pr_ratio(fake_get, tmp_path):
    """zhangjiayang6835-cyber/bounty-plaza, measured 2026-08-10: 198 closed
    under its own "Bounty" label, 23 with any linked PR.
    """
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _closed_page(198))
    fake_get.queue(200, _closed_page(23))
    result = payers.ever_closed_with(
        conn, "zhangjiayang6835-cyber/bounty-plaza", "Bounty", now=100.0)
    assert result == ClosedBounties(198, 23)


def test_a_failure_on_the_first_search_is_none_not_zero(fake_get, tmp_path):
    # "I could not look" must never render as "there are none": that is the
    # single distinction this whole pipeline is built against, and here it is the
    # difference between abstaining and rejecting a real payer's whole backlog.
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(403, {"message": "rate limited"})
    assert payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=100.0) is None


def test_a_failure_on_the_second_search_is_none_and_caches_nothing(fake_get, tmp_path):
    """Both numbers come from the same evidence-gathering operation, so a
    failure on EITHER half must discard the whole answer -- caching total=50
    with no with_pr would be exactly the "silent zero" trap this file exists
    to avoid, just shaped as a silent 100%-unlinked instead.
    """
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _closed_page(50))
    fake_get.queue(403, {"message": "rate limited"})
    assert payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=100.0) is None

    # Nothing was cached: the next call re-asks rather than returning a partial
    # or a stale answer.
    fake_get.queue(200, _closed_page(50))
    fake_get.queue(200, _closed_page(48))
    assert (payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=100.0)
            == ClosedBounties(50, 48))


def test_the_answer_is_cached_and_the_second_call_makes_no_request(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _closed_page(4))
    fake_get.queue(200, _closed_page(3))
    assert (payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=100.0)
            == ClosedBounties(4, 3))
    assert (payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=200.0)
            == ClosedBounties(4, 3))
    assert len(fake_get.calls) == 2  # both units spent once, not twice


def test_the_cache_expires(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _closed_page(4))
    fake_get.queue(200, _closed_page(3))
    fake_get.queue(200, _closed_page(9))
    fake_get.queue(200, _closed_page(8))
    payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=100.0)
    later = 100.0 + config.PAYER_CACHE_S + 1
    assert (payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=later)
            == ClosedBounties(9, 8))


def test_the_first_query_asks_for_closed_issues_in_that_repo_with_that_label(
        fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _closed_page(0))
    fake_get.queue(200, _closed_page(0))
    payers.ever_closed_with(conn, "acme/widgets", "💎 Bounty", now=100.0)
    _, params = fake_get.calls[0]
    assert params["q"] == 'repo:acme/widgets is:issue is:closed label:"💎 Bounty"'
    assert params["per_page"] == 1


def test_the_second_query_adds_linked_pr_to_the_same_shape(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _closed_page(0))
    fake_get.queue(200, _closed_page(0))
    payers.ever_closed_with(conn, "acme/widgets", "💎 Bounty", now=100.0)
    _, params = fake_get.calls[1]
    assert (params["q"] ==
            'repo:acme/widgets is:issue is:closed label:"💎 Bounty" linked:pr')
    assert params["per_page"] == 1


def test_reserves_two_search_units_because_it_makes_two_requests(fake_get, tmp_path):
    """total and with_pr each cost a search unit. Reserving only one would let
    the second request run past the budget guard's own accounting -- the same
    class of bug reserve_or_wait exists to make impossible.

    reserve_or_wait's own clock defaults to real wall time (ever_closed_with
    passes no `clock=`, exactly as it never did before this change), so the
    budget has to be pre-spent against `time.time()`, independently of the
    `now=` this test hands ever_closed_with for cache bookkeeping.
    """
    import time as time_mod

    conn = store.connect(tmp_path / "s.sqlite")
    # only 1 of 30 left in the real current window
    store.reserve(conn, "search", 29, now=time_mod.time())
    assert payers.ever_closed_with(conn, "acme/widgets", "Bounty", now=100.0) is None
    assert fake_get.calls == []  # refused before any request went out


def test_the_farm_check_does_not_buy_the_discovery_retry_ladder(fake_get, tmp_path,
                                                                monkeypatch):
    """retries=0, deliberately, and NOT the SEARCH_MAX_RETRIES default, for
    BOTH searches.

    The 60/120/240s ladder exists because abandoning a discovery page cost 420
    of 520 candidates. Abandoning a farm check costs one abstain on one repo,
    and 14 repos qualify once the 'open linked PR' rows are revived -- 14 x 420s
    is 98 minutes of a 3-hourly sweep spent buying nothing.
    """
    from sniper import github_api

    seen = []
    monkeypatch.setattr(
        github_api, "search_issues",
        lambda query, **kw: seen.append((query, kw)) or github_api.SearchResult(
            items=[], total_count=0, incomplete=False, pages_fetched=1))
    conn = store.connect(tmp_path / "s.sqlite")
    payers.ever_closed_with(conn, "farm/repo", "Bounty", now=100.0)
    assert len(seen) == 2
    assert all(kw["retries"] == 0 for _query, kw in seen)
    assert "linked:pr" not in seen[0][0]
    assert "linked:pr" in seen[1][0]


def test_the_farm_check_is_paced_like_a_search_page(fake_get, tmp_path, monkeypatch):
    """apply_payout_rejections fires one of these per qualifying repo, and 14
    qualify post-revive. Ten back-to-back requests tripped GitHub's secondary
    limit with 23 of 30 primary units unspent (measured 2026-08-08), and
    search_issues only paces between PAGES -- this asks for exactly one page
    each, twice, so both need their own pace.
    """
    from sniper import config

    monkeypatch.setattr(config, "SEARCH_PAGE_PAUSE_S", 3.0)
    slept = []
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _closed_page(0))
    fake_get.queue(200, _closed_page(0))
    payers.ever_closed_with(conn, "farm/repo", "Bounty", now=100.0,
                            sleep=slept.append)
    assert slept == [3.0, 3.0]


def test_a_cache_hit_pays_no_pause_because_it_makes_no_request(fake_get, tmp_path,
                                                                monkeypatch):
    from sniper import config

    monkeypatch.setattr(config, "SEARCH_PAGE_PAUSE_S", 3.0)
    slept = []
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _closed_page(4))
    fake_get.queue(200, _closed_page(3))
    payers.ever_closed_with(conn, "a/b", "Bounty", now=100.0, sleep=slept.append)
    payers.ever_closed_with(conn, "a/b", "Bounty", now=200.0, sleep=slept.append)
    assert slept == [3.0, 3.0]  # the second call answered from cache, paid nothing


def test_evidence_to_check_returns_the_label_payout_reject_would_judge():
    evidence = payers.evidence_to_check("farm/repo", ("Bounty", "$500"), 526,
                                        {"real/payer"})
    assert evidence.closed_label == "Bounty"
    assert evidence.check_rewarded is False  # no exact Algora label here


def test_evidence_to_check_flags_the_rewarded_lookup_for_the_exact_algora_label():
    """`check_rewarded` is keyed on the EXACT `payers.ALGORA_LABEL` string, not
    on bounty_label_of's looser "contains bounty" match -- microG's plain
    "bounty" label must never trigger a query whose only justification is
    Algora's own ledger, since microG never opted into Algora at all.
    """
    evidence = payers.evidence_to_check(
        "SecureBananaLabs/bug-bounty", ("💎 Bounty", "$9000"), 31, {"real/payer"})
    assert evidence.check_rewarded is True

    evidence = payers.evidence_to_check(
        "microg/GmsCore", ("enhancement", "bounty"), 1, {"real/payer"})
    assert evidence.check_rewarded is False


def test_evidence_to_check_flags_the_rewarded_lookup_below_the_open_count_threshold():
    """UnsafeLabs/Bounty-Hunters, measured 2026-08-10: 9 live candidates, one
    under UNPAID_REPO_THRESHOLD(10). The closed-ratio path abstains here (as
    it always did for a repo this size), but the Algora rewarded check is not
    gated on open_bounties_here at all -- it asks the repo directly, not a
    sample -- so it must still fire.
    """
    evidence = payers.evidence_to_check(
        "UnsafeLabs/Bounty-Hunters", ("💎 Bounty", "AI Agent friendly"), 9,
        {"real/payer"})
    assert evidence.closed_label is None
    assert evidence.check_rewarded is True


def test_evidence_to_check_flags_the_rewarded_lookup_even_with_no_roster():
    """The Algora rewarded check reads the REPO's own ledger, not the payer
    roster sample -- so unlike the closed-ratio path, it does not wait for
    known_payers to have succeeded at all.
    """
    evidence = payers.evidence_to_check(
        "SecureBananaLabs/bug-bounty", ("💎 Bounty",), 31, set())
    assert evidence.closed_label is None  # roster unavailable: ratio path abstains
    assert evidence.check_rewarded is True  # but this does not depend on the roster


def test_evidence_to_check_abstains_on_exactly_what_payout_reject_abstains_on():
    """One gate, two callers. A copy that drifted more RESTRICTIVE would hand
    payout_reject closed=None (or rewarded=None) where a real count was
    needed, and silently keep a farm -- which is why the conditions live in
    one function.
    """
    cases = [
        ("farm/repo", ("Bounty",), 526, set()),            # roster unavailable
        ("real/payer", ("Bounty",), 999, {"real/payer"}),  # proven payer
        ("some/repo", ("bug",), 526, {"real/payer"}),      # nothing marks a bounty
        ("small/repo", ("Bounty",), 2, {"real/payer"}),    # under the threshold
    ]
    for repo, labels, open_here, roster in cases:
        evidence = payers.evidence_to_check(repo, labels, open_here, roster)
        assert evidence.closed_label is None
        assert evidence.check_rewarded is False  # none of these carry the Algora label
        # ... and payout_reject abstains on the same input even when handed the
        # most damning closed evidence there is.
        assert payers.payout_reject(repo, labels, open_here, roster,
                                    closed=ClosedBounties(0, 0)) is None


def test_rejects_a_farm_using_its_own_label():
    reason = payers.payout_reject("farm/repo", ("Bounty",), 526, {"real/payer"},
                                  closed=ClosedBounties(0, 0))
    assert reason == "no payout history (526 open bounties here, 0 ever closed)"


def test_rejects_a_farm_that_bulk_closes_without_merging():
    """zhangjiayang6835-cyber/bounty-plaza grounds this: 527 open, 198 closed
    under its own "Bounty" label -- enough to survive the old ">0 ever closed"
    rule -- but only 23 with any linked PR (12%). Three sampled closures
    (#12, #13, #16) landed 2026-07-12T10:43:51Z / :55Z / :58Z, seven seconds
    apart, none assigned, one titled "...[bounty][funding-pending]:...". That
    is a bulk close, not payout history.
    """
    reason = payers.payout_reject(
        "zhangjiayang6835-cyber/bounty-plaza", ("Bounty",), 527,
        {"activepieces/activepieces"}, closed=ClosedBounties(198, 23))
    assert reason == "no payout history (198 closed here, only 23 with any linked PR)"


def test_keeps_a_repo_whose_closures_are_mostly_linked_to_a_pr():
    """commaai/opendbc grounds this: no Algora label anywhere, yet 7 closed
    bounties including a $4,000 port, 4 of them with a linked PR (57%). It
    would never appear in Algora's rewarded roster and, on labels alone, looks
    identical to a farm -- but a 57% link ratio is direct evidence a real
    programme is running, well clear of CLOSED_PR_RATIO_FLOOR (0.34).
    """
    assert payers.payout_reject("commaai/opendbc", ("Bounty",), 30,
                                {"real/payer"},
                                closed=ClosedBounties(7, 4)) is None


def test_zero_closed_never_divides_by_zero(fake_get, tmp_path):
    # total=0 must short-circuit before the with_pr/total ratio is computed --
    # a ZeroDivisionError here would crash a whole sweep on the first farm found.
    reason = payers.payout_reject("farm/repo", ("Bounty",), 526, {"real/payer"},
                                  closed=ClosedBounties(0, 0))
    assert reason == "no payout history (526 open bounties here, 0 ever closed)"


def test_a_single_unlinked_closure_is_too_small_a_sample_and_abstains():
    """Reverses the OLD "no minimum-sample carve-out" decision.

    That decision assumed a farm past UNPAID_REPO_THRESHOLD had nothing left
    to hide behind -- but measured 2026-08-10, SecureBananaLabs/bug-bounty
    proved the opposite: ONE closed issue with a linked PR (1/1 = 100%) sailed
    straight through the old ratio-only check while the repo had 0 issues ever
    marked 💰 Rewarded and 31 other open bounties untouched. A single closure,
    in either direction, is not a sample -- it is a coin flip. Below
    config.CLOSED_SAMPLE_MIN(5), payout_reject now abstains rather than trust
    a ratio this thin, same as it always abstained on closed=None.
    """
    reason = payers.payout_reject("farm/repo", ("Bounty",), 526, {"real/payer"},
                                  closed=ClosedBounties(1, 0))
    assert reason is None


def test_the_ratio_floor_is_a_strict_less_than():
    # Exactly at the floor is kept; one PR short of it is rejected. Both use
    # the same total so only the with_pr count -- and therefore the ratio --
    # differs between them.
    assert payers.payout_reject("at/floor", ("Bounty",), 100, {"real/payer"},
                                closed=ClosedBounties(100, 34)) is None
    reason = payers.payout_reject("below/floor", ("Bounty",), 100, {"real/payer"},
                                  closed=ClosedBounties(100, 33))
    assert reason == "no payout history (100 closed here, only 33 with any linked PR)"


def test_abstains_when_the_closed_evidence_is_unknown():
    assert payers.payout_reject("farm/repo", ("Bounty",), 526, {"real/payer"},
                                closed=None) is None


def test_abstains_on_a_known_payer_without_asking():
    assert payers.payout_reject("real/payer", ("Bounty",), 999, {"real/payer"},
                                closed=ClosedBounties(0, 0)) is None


def test_abstains_below_the_open_count_threshold():
    # One or two unclosed bounties is a new project, not a farm.
    assert payers.payout_reject("small/repo", ("Bounty",), 2, {"real/payer"},
                                closed=ClosedBounties(0, 0)) is None


def test_abstains_when_no_label_marks_it_as_a_bounty():
    assert payers.payout_reject("some/repo", ("bug",), 526, {"real/payer"},
                                closed=ClosedBounties(0, 0)) is None


def test_abstains_when_the_payer_roster_is_unavailable():
    # An empty roster means the lookup failed, not that nobody pays.
    assert payers.payout_reject("farm/repo", ("Bounty",), 526, set(),
                                closed=ClosedBounties(0, 0)) is None


# --- Algora's own ledger: a repo's rewarded count, direct not sampled -------
#
# known_payers is a SAMPLE: the 43 most-recent payers across all of Algora, so
# a repo's absence from it is weak evidence -- it may simply not have paid
# RECENTLY. rewarded_count asks GitHub about ONE repo directly, so a zero here
# is not a sample running dry, it is the platform's own record saying nobody
# was ever paid. Measured 2026-08-10:
#
#   repo                            live   💎 Bounty  0 rewarded  closed w/PR
#   SecureBananaLabs/bug-bounty       31       yes         yes        1 of 1
#   UnsafeLabs/Bounty-Hunters          9       yes         yes        0 of 0
#
# SecureBananaLabs passed the OLD ratio check at a meaningless 1/1 = 100%.
# UnsafeLabs sat one candidate under UNPAID_REPO_THRESHOLD(10), so the OLD
# gate abstained before ever asking. Both wear Algora's exact label; both have
# never been rewarded once.


def test_securebananalabs_shape_is_rejected_despite_the_ratio_looking_perfect():
    """31 live candidates, 0 issues ever marked 💰 Rewarded, one closed 💎
    Bounty issue with a linked PR -- 1/1 = 100%, which is exactly why a ratio
    alone was not enough. Algora's own ledger overrides it.
    """
    reason = payers.payout_reject(
        "SecureBananaLabs/bug-bounty", ("💎 Bounty", "$9000"), 31,
        {"some/other-payer"}, closed=ClosedBounties(1, 1), rewarded=0)
    assert reason is not None
    assert "Rewarded" in reason


def test_unsafelabs_shape_is_rejected_below_the_open_count_threshold():
    """9 live candidates -- one under UNPAID_REPO_THRESHOLD(10) -- carrying
    💎 Bounty among labels that also include 'AI Agent friendly' and 'AI only
    allowed - no humans', and 0 issues ever marked 💰 Rewarded. The OLD gate
    abstained purely because 9 < 10; the Algora rewarded check does not wait
    for that threshold at all, because it reads the repo directly rather than
    comparing it against a sample.
    """
    reason = payers.payout_reject(
        "UnsafeLabs/Bounty-Hunters",
        ("💎 Bounty", "AI Agent friendly", "AI only allowed - no humans"),
        9, {"some/other-payer"}, closed=None, rewarded=0)
    assert reason is not None


def test_microg_shape_is_kept_it_never_wears_the_algora_label():
    """microg/GmsCore: 14,170 stars, labels ['enhancement', 'bounty'], a real
    $1,340 bounty, 0 issues ever marked 💰 Rewarded -- but it never wears
    Algora's exact 💎 Bounty label, so its silence on Algora's own platform
    means nothing at all: it never opted in. Only one bounty is open here,
    well under UNPAID_REPO_THRESHOLD, so the closed-ratio path abstains too.
    This is the asymmetry the whole rule depends on: absence of the label
    means absence of a record to be silent about.
    """
    reason = payers.payout_reject(
        "microg/GmsCore", ("enhancement", "bounty"), 1, {"some/other-payer"},
        closed=None, rewarded=0)
    assert reason is None


def test_activepieces_shape_is_kept_when_rewarded_is_nonzero():
    """activepieces/activepieces: wears 💎 Bounty, rewarded 150 times overall.
    Even off the payer-roster sample and past UNPAID_REPO_THRESHOLD, a nonzero
    rewarded count keeps it -- and its closed ratio (170/173, 98%) keeps it on
    that ground too.
    """
    reason = payers.payout_reject(
        "activepieces/activepieces", ("💎 Bounty", "$200"), 173,
        {"some/other-payer"}, closed=ClosedBounties(173, 170), rewarded=150)
    assert reason is None


def test_opendbc_shape_is_kept_above_the_sample_minimum():
    """commaai/opendbc: no Algora label anywhere, runs its own programme -- 7
    closed bounties including a $4,000 port, 4 with a linked PR (57%). 7 is at
    or above config.CLOSED_SAMPLE_MIN(5), so the ratio is trusted outright and
    clears CLOSED_PR_RATIO_FLOOR (0.34) comfortably. This is the shape the
    sample floor exists NOT to catch -- see
    test_a_tiny_closed_sample_abstains_rather_than_rejects for the boundary
    case where the floor itself has to fire.
    """
    assert payers.payout_reject(
        "commaai/opendbc", ("Bounty",), 30, {"some/other-payer"},
        closed=ClosedBounties(7, 4)) is None


def test_a_tiny_closed_sample_abstains_rather_than_rejects():
    """Get the direction right, and this is the important part: BELOW
    config.CLOSED_SAMPLE_MIN(5), payout_reject must abstain, not reject,
    however bad the ratio looks. SecureBananaLabs/bug-bounty is exactly why a
    minimum that rejected small samples would be dangerous -- 1/1 (100%)
    sailed through the OLD ratio-only check, so flipping straight to "reject
    every tiny sample" would only trade one false verdict for its opposite.
    This uses a synthetic 0-with-PR closure at total=4 (still below the
    floor) to prove the abstain branch fires even when the ratio, if it were
    trusted, would look damning.
    """
    reason = payers.payout_reject(
        "small-sample/repo", ("Bounty",), 526, {"some/other-payer"},
        closed=ClosedBounties(4, 0))
    assert reason is None


def test_bounty_plaza_shape_is_still_rejected_above_the_sample_minimum():
    """zhangjiayang6835-cyber/bounty-plaza: 198 closed, 23 with a linked PR
    (12%) -- 198 is far above config.CLOSED_SAMPLE_MIN(5), so the sample
    floor does not shield it; the ratio, well below CLOSED_PR_RATIO_FLOOR,
    rejects exactly as before the floor existed.
    """
    reason = payers.payout_reject(
        "zhangjiayang6835-cyber/bounty-plaza", ("Bounty",), 527,
        {"some/other-payer"}, closed=ClosedBounties(198, 23))
    assert reason == "no payout history (198 closed here, only 23 with any linked PR)"


def test_a_failed_rewarded_lookup_abstains():
    """rewarded=None means the lookup could not run (budget exhausted, the
    request failed, or it was simply never bought) -- exactly like closed=None
    above, this must never be read as "zero", or a throttled rewarded_count
    call would reject every 💎 Bounty repo in a sweep whenever search quota
    ran thin.
    """
    assert payers.payout_reject(
        "SecureBananaLabs/bug-bounty", ("💎 Bounty",), 31,
        {"some/other-payer"}, closed=ClosedBounties(1, 1), rewarded=None) is None


# --- rewarded_count: Algora's own ledger for one repo, one search -----------


def _rewarded_page(total_count: int):
    return {"total_count": total_count, "incomplete_results": False, "items": []}


def test_rewarded_count_returns_the_repos_own_rewarded_total(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _rewarded_page(150))
    assert payers.rewarded_count(
        conn, "activepieces/activepieces", now=100.0) == 150


def test_rewarded_count_of_zero_is_a_real_zero_not_a_failure(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _rewarded_page(0))
    assert payers.rewarded_count(
        conn, "SecureBananaLabs/bug-bounty", now=100.0) == 0


def test_rewarded_count_asks_for_the_rewarded_label_in_that_repo(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _rewarded_page(0))
    payers.rewarded_count(conn, "UnsafeLabs/Bounty-Hunters", now=100.0)
    _, params = fake_get.calls[0]
    assert params["q"] == 'repo:UnsafeLabs/Bounty-Hunters is:issue label:"💰 Rewarded"'
    assert params["per_page"] == 1


def test_rewarded_count_a_failure_is_none_not_zero(fake_get, tmp_path):
    # "I could not look" must never render as "there are none" -- the same
    # distinction ever_closed_with exists to protect.
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(403, {"message": "rate limited"})
    assert payers.rewarded_count(conn, "acme/widgets", now=100.0) is None


def test_rewarded_count_is_cached_so_a_second_call_makes_no_request(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _rewarded_page(0))
    assert payers.rewarded_count(conn, "acme/widgets", now=100.0) == 0
    assert payers.rewarded_count(conn, "acme/widgets", now=200.0) == 0
    assert len(fake_get.calls) == 1


def test_rewarded_count_cache_expires(fake_get, tmp_path):
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _rewarded_page(0))
    payers.rewarded_count(conn, "acme/widgets", now=100.0)
    fake_get.queue(200, _rewarded_page(3))
    later = 100.0 + config.PAYER_CACHE_S + 1
    assert payers.rewarded_count(conn, "acme/widgets", now=later) == 3


def test_rewarded_count_reserves_one_search_unit_and_refuses_when_the_pool_is_spent(
        fake_get, tmp_path):
    import time as time_mod

    conn = store.connect(tmp_path / "s.sqlite")
    store.reserve(conn, "search", 30, now=time_mod.time())  # pool is empty
    assert payers.rewarded_count(conn, "acme/widgets", now=100.0) is None
    assert fake_get.calls == []  # refused before any request went out


def test_rewarded_count_does_not_buy_the_discovery_retry_ladder(
        fake_get, tmp_path, monkeypatch):
    """retries=0, deliberately, same reasoning as ever_closed_with: abandoning
    this one costs an abstain on one repo, not a whole discovery page.
    """
    from sniper import github_api

    seen = []
    monkeypatch.setattr(
        github_api, "search_issues",
        lambda query, **kw: seen.append((query, kw)) or github_api.SearchResult(
            items=[], total_count=0, incomplete=False, pages_fetched=1))
    conn = store.connect(tmp_path / "s.sqlite")
    payers.rewarded_count(conn, "acme/widgets", now=100.0)
    assert len(seen) == 1
    assert seen[0][1]["retries"] == 0


def test_rewarded_count_is_paced_like_a_search_page(fake_get, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SEARCH_PAGE_PAUSE_S", 3.0)
    slept = []
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _rewarded_page(0))
    payers.rewarded_count(conn, "acme/widgets", now=100.0, sleep=slept.append)
    assert slept == [3.0]


def test_rewarded_count_a_cache_hit_pays_no_pause(fake_get, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SEARCH_PAGE_PAUSE_S", 3.0)
    slept = []
    conn = store.connect(tmp_path / "s.sqlite")
    fake_get.queue(200, _rewarded_page(4))
    payers.rewarded_count(conn, "a/b", now=100.0, sleep=slept.append)
    payers.rewarded_count(conn, "a/b", now=200.0, sleep=slept.append)
    assert slept == [3.0]  # the second call answered from cache, paid nothing
