"""Candidate construction and amount parsing.

Bounty amounts live in labels ($780, $1k, $1.2k). A single issue carrying more
than one distinct amount is the sharpest spam signal in the data -- see
SecureBananaLabs/bug-bounty#11398, which carries $780, $1k AND $1.2k -- so
amounts_in() reports the whole set and lets filters.py judge it.
"""

import pytest

from sniper.models import Candidate, amounts_in, parse_amount_label, repo_from_url
from tests.conftest import load_fixture


@pytest.mark.parametrize("label,expected", [
    ("$780", 780),
    ("$1k", 1000),
    ("$1K", 1000),
    ("$1.2k", 1200),
    ("$1,200", 1200),
    ("$ 50", 50),
    ("$1", 1),
    ("bounty", None),
    ("💎 Bounty", None),
    ("$", None),
    ("", None),
    ("100", None),
    ("$abc", None),
    ("$12,345", 12345),
    # Malformed. These must come back None -- never an exception, and never a
    # plausible wrong number. A looser regex raised on the first and silently
    # returned 123 for the second.
    ("$1.2.3", None),
    ("$1,2,3", None),
    ("$1,2345", None),
])
def test_parse_amount_label(label, expected):
    assert parse_amount_label(label) == expected


def test_amounts_in_is_distinct_and_ascending():
    assert amounts_in(["$1k", "bug", "$780", "$1.2k", "$1000"]) == (780, 1000, 1200)


def test_amounts_in_empty_when_no_amount_labels():
    assert amounts_in(["💎 Bounty", "help wanted"]) == ()


def test_a_malformed_label_never_crashes_candidate_construction():
    # Every label on every issue goes through here unattended. One bad label must
    # not take down the run.
    assert amounts_in(["$1.2.3", "$1,2,3", "$780", "bounty"]) == (780,)


def test_repo_from_url():
    assert repo_from_url("https://api.github.com/repos/acme/widgets") == "acme/widgets"


def _item(**over):
    base = {
        "number": 7,
        "title": "Fix the thing",
        "state": "open",
        "comments": 3,
        "created_at": "2026-05-16T08:50:23Z",
        "updated_at": "2026-08-05T06:17:10Z",
        "html_url": "https://github.com/acme/widgets/issues/7",
        "repository_url": "https://api.github.com/repos/acme/widgets",
        "assignee": None,
        "labels": [{"name": "💎 Bounty"}, {"name": "$780"}],
    }
    base.update(over)
    return base


def test_from_search_item_extracts_repo_labels_and_amount():
    cand = Candidate.from_search_item("github-labels", _item())
    assert cand.source == "github-labels"
    assert cand.repo == "acme/widgets"
    assert cand.number == 7
    assert cand.key == ("github-labels", "acme/widgets", 7)
    assert cand.labels == ("💎 Bounty", "$780")
    assert cand.amount_usd == 780
    assert cand.amounts_seen == (780,)
    assert cand.has_assignee is False
    assert cand.comments == 3
    assert cand.url == "https://github.com/acme/widgets/issues/7"


def test_multiple_amounts_leave_amount_none_but_are_all_recorded():
    cand = Candidate.from_search_item("github-labels", _item(
        labels=[{"name": "$780"}, {"name": "$1k"}, {"name": "$1.2k"}]))
    assert cand.amounts_seen == (780, 1000, 1200)
    assert cand.amount_usd is None


def test_no_amount_label_is_none_not_an_error():
    cand = Candidate.from_search_item("github-labels", _item(labels=[{"name": "💎 Bounty"}]))
    assert cand.amount_usd is None
    assert cand.amounts_seen == ()


def test_assignee_is_detected_from_either_field():
    assert Candidate.from_search_item("s", _item(assignee={"login": "x"})).has_assignee is True
    assert Candidate.from_search_item("s", _item(assignees=[{"login": "x"}])).has_assignee is True


def test_repo_detail_fields_start_none_and_are_filled_by_with_repo_detail():
    cand = Candidate.from_search_item("github-labels", _item())
    assert (cand.fork, cand.archived, cand.stars, cand.pushed_at, cand.language) == (
        None, None, None, None, None)
    enriched = cand.with_repo_detail({
        "fork": False, "archived": False, "stargazers_count": 1200,
        "pushed_at": "2026-08-01T00:00:00Z", "language": "Python"})
    assert enriched.fork is False
    assert enriched.stars == 1200
    assert enriched.language == "Python"
    assert enriched.amount_usd == 780  # untouched
    assert cand.stars is None          # original is frozen and unmodified


def test_with_score_returns_a_new_candidate():
    cand = Candidate.from_search_item("github-labels", _item())
    assert cand.score is None
    assert cand.with_score(12.5).score == 12.5
    assert cand.score is None


def test_from_issue_honours_an_amount_override():
    payload = _item()
    payload.pop("repository_url")
    cand = Candidate.from_issue("expensify", "Expensify/App", payload, amount_usd=250)
    assert cand.repo == "Expensify/App"
    assert cand.amount_usd == 250
    # The override decides amount_usd; it does not erase what the labels said.
    # Suppressing this would hide an override that disagrees with the issue.
    assert cand.amounts_seen == (780,)


def test_an_override_on_a_label_free_issue_records_no_amounts():
    payload = _item(labels=[{"name": "Help Wanted"}])
    payload.pop("repository_url")
    cand = Candidate.from_issue("expensify", "Expensify/App", payload, amount_usd=250)
    assert cand.amount_usd == 250
    assert cand.amounts_seen == ()


def test_age_days_counts_from_created_at():
    import datetime as dt
    now = dt.datetime(2026, 8, 5, tzinfo=dt.timezone.utc).timestamp()
    cand = Candidate.from_search_item("s", _item(created_at="2026-07-06T00:00:00Z"))
    assert cand.age_days(now=now) == 30


def test_every_real_search_item_builds_a_candidate():
    items = load_fixture("search_bounty_page1")["items"]
    cands = [Candidate.from_search_item("github-labels", i) for i in items]
    assert len(cands) == len(items)
    assert all("/" in c.repo for c in cands)
    assert all(isinstance(c.number, int) for c in cands)
    assert all(c.state == "open" for c in cands)


def test_the_spam_farm_issue_shows_three_distinct_amounts():
    items = load_fixture("search_spam_farm")["items"]
    issue = next(i for i in items if i["number"] == 11398)
    cand = Candidate.from_search_item("github-labels", issue)
    assert cand.amounts_seen == (780, 1000, 1200)
    assert cand.amount_usd is None


def test_a_title_amount_is_used_when_the_labels_say_nothing():
    """commaai/opendbc#3426 is "[$10k bounty] Ford F-150 2026 (TRON) support".

    Real, well-funded, and it carries no $-label. Scoring those at zero buried
    genuine work underneath a farm whose issues happen to be labelled properly.
    """
    cand = Candidate.from_search_item("github-labels", _item(
        title="[$10k bounty] Ford F-150 2026 (TRON) support",
        labels=[{"name": "bounty"}]))
    assert cand.amount_usd == 10000


def test_a_label_amount_always_beats_the_title():
    cand = Candidate.from_search_item("github-labels", _item(
        title="[$10k bounty] something", labels=[{"name": "$500"}]))
    assert cand.amount_usd == 500


def test_an_ambiguous_title_is_not_guessed_at():
    # Two figures in prose is exactly as untrustworthy as two $-labels.
    cand = Candidate.from_search_item("github-labels", _item(
        title="[$500 bounty] plus a $50 gift card", labels=[{"name": "bounty"}]))
    assert cand.amount_usd is None


def test_a_title_with_no_amount_stays_none():
    cand = Candidate.from_search_item("github-labels", _item(
        title="Fix the thing", labels=[{"name": "bounty"}]))
    assert cand.amount_usd is None


def test_a_body_amount_is_used_when_labels_and_title_are_silent():
    """543 of 748 live candidates had no amount in labels or title, measured.

    body already arrives in the search payload, so reading it is free.
    """
    cand = Candidate.from_search_item("github-labels", _item(
        title="Add wallet integration", labels=[{"name": "bounty"}],
        body="We are offering a bounty of $500 for this. Steps to reproduce..."))
    assert cand.amount_usd == 500


def test_a_bare_dollar_figure_in_a_body_is_not_treated_as_a_prize():
    # Bodies are long prose. "$5" is far more often a price or a gas fee.
    cand = Candidate.from_search_item("github-labels", _item(
        title="Checkout is broken", labels=[{"name": "bounty"}],
        body="The cart shows $5 but charges $7. Expected $5."))
    assert cand.amount_usd is None


def test_the_title_still_beats_the_body():
    cand = Candidate.from_search_item("github-labels", _item(
        title="[$900 bounty] fix it", labels=[{"name": "bounty"}],
        body="bounty of $100 maybe"))
    assert cand.amount_usd == 900


def test_from_issue_records_assignee_logins():
    payload = {"number": 1, "title": "t", "labels": [],
               "assignees": [{"login": "alice"}, {"login": "Bob"}]}
    cand = Candidate.from_issue("github-labels", "acme/widgets", payload)
    assert cand.assignees == ("alice", "Bob")
    assert cand.has_assignee is True


def test_from_issue_records_the_legacy_single_assignee_field_too():
    # GitHub still emits `assignee` alongside `assignees`; a payload carrying
    # only the singular form must not produce an empty login list, or the
    # claim test in linked_pr_state silently never fires.
    payload = {"number": 1, "title": "t", "labels": [],
               "assignee": {"login": "carol"}}
    cand = Candidate.from_issue("github-labels", "acme/widgets", payload)
    assert cand.assignees == ("carol",)


def test_from_issue_deduplicates_assignees_preserving_order():
    payload = {"number": 1, "title": "t", "labels": [],
               "assignee": {"login": "carol"},
               "assignees": [{"login": "carol"}, {"login": "dave"}]}
    cand = Candidate.from_issue("github-labels", "acme/widgets", payload)
    assert cand.assignees == ("carol", "dave")


def test_unassigned_issue_has_no_logins_and_no_flag():
    payload = {"number": 1, "title": "t", "labels": []}
    cand = Candidate.from_issue("github-labels", "acme/widgets", payload)
    assert cand.assignees == ()
    assert cand.has_assignee is False


def test_open_pr_count_starts_unknown_and_is_set_explicitly():
    payload = {"number": 1, "title": "t", "labels": []}
    cand = Candidate.from_issue("github-labels", "acme/widgets", payload)
    assert cand.open_pr_count is None
    assert cand.with_linked_prs(2).open_pr_count == 2
    assert cand.with_linked_prs(0).open_pr_count == 0


def test_repo_detail_captures_the_repos_own_age_and_backlog():
    # Both arrive in the SAME payload the enrichment already fetches, so this
    # costs nothing. repo_created_at is the REPOSITORY's birthday -- created_at
    # on the Candidate is the issue's, and conflating them would rate a
    # month-old farm as a decade-old project.
    payload = {"fork": False, "archived": False, "stargazers_count": 5,
               "pushed_at": "2026-08-09T00:00:00Z", "language": "Python",
               "created_at": "2026-07-08T15:34:56Z", "open_issues": 526}
    cand = Candidate.from_issue("github-labels", "farm/repo",
                                {"number": 1, "title": "t", "labels": []})
    enriched = cand.with_repo_detail(payload)
    assert enriched.repo_created_at == "2026-07-08T15:34:56Z"
    assert enriched.repo_open_issues == 526
    assert enriched.created_at != enriched.repo_created_at
