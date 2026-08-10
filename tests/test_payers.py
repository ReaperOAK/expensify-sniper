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


def test_label_to_check_returns_the_label_payout_reject_would_judge():
    assert payers.label_to_check("farm/repo", ("Bounty", "$500"), 526,
                                 {"real/payer"}) == "Bounty"


def test_label_to_check_abstains_on_exactly_what_payout_reject_abstains_on():
    """One gate, two callers. A copy that drifted more RESTRICTIVE would hand
    payout_reject closed=None where a real count was needed, and silently
    keep a farm -- which is why the conditions live in one function.
    """
    cases = [
        ("farm/repo", ("Bounty",), 526, set()),            # roster unavailable
        ("real/payer", ("Bounty",), 999, {"real/payer"}),  # proven payer
        ("some/repo", ("bug",), 526, {"real/payer"}),      # nothing marks a bounty
        ("small/repo", ("Bounty",), 2, {"real/payer"}),    # under the threshold
    ]
    for repo, labels, open_here, roster in cases:
        assert payers.label_to_check(repo, labels, open_here, roster) is None
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


def test_a_single_unlinked_closure_is_the_weakest_evidence_and_is_rejected():
    """Design decision (see farm-ratio-report.md): no minimum-sample carve-out.

    payout_reject only ever runs once a repo already clears
    UNPAID_REPO_THRESHOLD (10) open bounties -- label_to_check's own gate is
    already the "benefit of the doubt" boundary for a small/new project. Once a
    repo is advertising ten-plus unclaimed bounties, one closure with no linked
    PR is not stronger evidence of a real programme than zero closures would be
    -- it is a single unmerged bulk-close away from looking exactly like
    bounty-plaza. The module's own asymmetric-risk framing (permissive drift
    wastes quota; restrictive drift only shrinks the board for a repo already
    shaped like a farm) says reject.
    """
    reason = payers.payout_reject("farm/repo", ("Bounty",), 526, {"real/payer"},
                                  closed=ClosedBounties(1, 0))
    assert reason == "no payout history (1 closed here, only 0 with any linked PR)"


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
