"""Rejection rules, in cost order.

free_reject() decides from the search payload alone -- zero API calls -- so it
must run before any enrichment. repo_reject() needs the repo-detail call, which
is why fork/archived cannot be free rejects -- search result items carry no
repository object (measured 2026-08-05).

Both return a REASON string rather than a bool, because a candidate that
vanished silently is indistinguishable from one that was never found.
"""

import pytest

from sniper import filters
from sniper.models import Candidate
from tests.conftest import load_fixture


def _cand(**over):
    base = {
        "number": 7, "title": "t", "state": "open", "comments": 1,
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-08-01T00:00:00Z",
        "html_url": "u", "repository_url": "https://api.github.com/repos/acme/widgets",
        "assignee": None, "labels": [{"name": "$780"}],
    }
    base.update(over)
    return Candidate.from_search_item("github-labels", base)


def test_a_clean_candidate_is_kept():
    assert filters.free_reject(_cand()) is None


def test_closed_is_rejected():
    assert filters.free_reject(_cand(state="closed")) == "closed"


def test_assigned_is_rejected():
    assert filters.free_reject(_cand(assignee={"login": "x"})) == "assigned"


def test_paid_label_is_rejected_case_insensitively():
    assert filters.free_reject(
        _cand(labels=[{"name": "$780"}, {"name": "💰 REWARDED"}])) == "already rewarded"


def test_paid_labels_are_configurable():
    reason = filters.free_reject(
        _cand(labels=[{"name": "$780"}, {"name": "Paid Out"}]),
        paid_labels={"paid out"})
    assert reason == "already rewarded"


def test_multiple_distinct_amounts_is_rejected_as_spam():
    reason = filters.free_reject(
        _cand(labels=[{"name": "$780"}, {"name": "$1k"}, {"name": "$1.2k"}]))
    assert reason == "multiple amount labels"


def test_amount_below_the_floor_is_rejected():
    assert filters.free_reject(_cand(labels=[{"name": "$25"}]), floor=50) == "below $50 floor"


def test_amount_exactly_at_the_floor_is_kept():
    assert filters.free_reject(_cand(labels=[{"name": "$50"}]), floor=50) is None


def test_a_missing_amount_does_not_reject():
    assert filters.free_reject(_cand(labels=[{"name": "💎 Bounty"}])) is None


def test_free_reject_never_looks_at_repo_detail():
    # fork/archived are None here (unenriched) and must not cause a reject.
    cand = _cand()
    assert cand.fork is None and cand.archived is None
    assert filters.free_reject(cand) is None


def test_repo_reject_rejects_a_fork():
    cand = _cand().with_repo_detail(
        {"fork": True, "archived": False, "stargazers_count": 0,
         "pushed_at": "2026-06-11T04:44:17Z", "language": "Rust"})
    assert filters.repo_reject(cand) == "fork"


def test_repo_reject_rejects_an_archived_repo():
    cand = _cand().with_repo_detail(
        {"fork": False, "archived": True, "stargazers_count": 900,
         "pushed_at": "2026-06-11T04:44:17Z", "language": "Go"})
    assert filters.repo_reject(cand) == "archived repo"


def test_repo_reject_keeps_a_healthy_repo():
    cand = _cand().with_repo_detail(
        {"fork": False, "archived": False, "stargazers_count": 12,
         "pushed_at": "2026-08-05T06:20:46Z", "language": "Solidity"})
    assert filters.repo_reject(cand) is None


def test_repo_reject_refuses_to_guess_on_an_unenriched_candidate():
    with pytest.raises(ValueError, match="not enriched"):
        filters.repo_reject(_cand())


def test_regression_the_spam_farm_issue_is_rejected_free():
    issue = next(i for i in load_fixture("search_spam_farm")["items"]
                 if i["number"] == 11398)
    cand = Candidate.from_search_item("github-labels", issue)
    assert filters.free_reject(cand) == "multiple amount labels"


def test_regression_the_rewarded_fork_is_rejected_before_enrichment():
    items = load_fixture("search_fork_rewarded")["items"]
    cands = [Candidate.from_search_item("github-labels", i) for i in items]
    assert cands, "fixture is empty"
    # Both issues carry 💰 Rewarded, so they never reach the repo-detail call.
    assert all(filters.free_reject(c) == "already rewarded" for c in cands)


def test_regression_the_fork_would_also_be_rejected_at_the_repo_stage():
    items = load_fixture("search_fork_rewarded")["items"]
    cand = Candidate.from_search_item("github-labels", items[0]).with_repo_detail(
        load_fixture("repo_fork"))
    assert filters.repo_reject(cand) == "fork"
