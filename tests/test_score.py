"""score = amount_usd / (1 + comments) x repo_health.

`comments` arrives free in the search payload, so the competition proxy costs no
extra call. repo_health is the product of a stars band and a recent-activity
band -- a bounty in an abandoned or unserious repo is likelier to go unmerged or
unpaid even when the work is correct.

Age is deliberately NOT a term. Essentially the whole inventory is old (1 of 521
created in the previous 30 days), so an age term discriminates nothing, and
"old with zero comments" is genuinely ambiguous between hard and overlooked.
Encoding a guess would manufacture signal that is not in the data.
"""

import datetime as dt

import pytest

from sniper import score as scoring
from sniper.models import Candidate

NOW = dt.datetime(2026, 8, 5, tzinfo=dt.timezone.utc).timestamp()


def _at(days_ago: int) -> str:
    when = dt.datetime.fromtimestamp(NOW, tz=dt.timezone.utc) - dt.timedelta(days=days_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("stars,expected", [
    (10_000, 1.0), (500, 1.0), (499, 0.8), (100, 0.8),
    (99, 0.6), (10, 0.6), (9, 0.5), (0, 0.5),
])
def test_stars_bands(stars, expected):
    assert scoring.repo_health(stars, _at(1), now=NOW) == pytest.approx(expected)


def test_recent_push_is_full_credit_and_stale_is_penalised():
    assert scoring.repo_health(1000, _at(89), now=NOW) == pytest.approx(1.0)
    assert scoring.repo_health(1000, _at(91), now=NOW) == pytest.approx(0.7)


def test_health_is_the_product_of_both_factors():
    # Corrects the spec's stated [0.5, 1.0]: the factors bottom out at 0.5 x 0.7.
    assert scoring.repo_health(0, _at(365), now=NOW) == pytest.approx(0.35)
    assert scoring.repo_health(10_000, _at(0), now=NOW) == pytest.approx(1.0)


def _cand(*, amount_label="$1000", comments=0, stars=1000, pushed_days=1):
    payload = {
        "number": 7, "title": "t", "state": "open", "comments": comments,
        "created_at": _at(400), "updated_at": _at(1), "html_url": "u",
        "repository_url": "https://api.github.com/repos/acme/widgets",
        "assignee": None, "labels": [{"name": amount_label}],
    }
    return Candidate.from_search_item("github-labels", payload).with_repo_detail(
        {"fork": False, "archived": False, "stargazers_count": stars,
         "pushed_at": _at(pushed_days), "language": "Python"})


def test_score_is_amount_over_one_plus_comments_times_health():
    assert scoring.score(_cand(amount_label="$1000", comments=0), now=NOW) == pytest.approx(1000.0)
    assert scoring.score(_cand(amount_label="$1000", comments=9), now=NOW) == pytest.approx(100.0)


def test_health_multiplies_through():
    assert scoring.score(
        _cand(amount_label="$1000", comments=0, stars=5, pushed_days=400),
        now=NOW) == pytest.approx(350.0)


def test_a_missing_amount_scores_zero_so_it_sorts_last():
    cand = _cand(amount_label="💎 Bounty")
    assert cand.amount_usd is None
    assert scoring.score(cand, now=NOW) == 0.0


def test_age_is_not_a_term():
    import dataclasses

    old = _cand(amount_label="$1000", comments=0)
    young = dataclasses.replace(old, created_at=_at(1))
    assert old.age_days(now=NOW) != young.age_days(now=NOW)
    assert scoring.score(old, now=NOW) == scoring.score(young, now=NOW)


def test_scoring_an_unenriched_candidate_raises_rather_than_guessing():
    bare = Candidate.from_search_item("github-labels", {
        "number": 1, "title": "t", "state": "open", "comments": 0,
        "created_at": _at(10), "updated_at": _at(1), "html_url": "u",
        "repository_url": "https://api.github.com/repos/acme/widgets",
        "assignee": None, "labels": [{"name": "$500"}]})
    with pytest.raises(ValueError, match="not enriched"):
        scoring.score(bare, now=NOW)


def test_a_cheaper_bounty_in_a_healthy_quiet_repo_can_beat_a_crowded_rich_one():
    quiet = _cand(amount_label="$500", comments=0, stars=1000, pushed_days=1)
    crowded = _cand(amount_label="$2000", comments=30, stars=1000, pushed_days=1)
    assert scoring.score(quiet, now=NOW) > scoring.score(crowded, now=NOW)


def test_open_prs_count_as_competition():
    # An open PR is a rival who has already written code, so it costs more than
    # a comment. It is not a wall: on Expensify selection is by proposal, and on
    # Algora only a merge wins, so a better entrant can still take it.
    base = _cand(amount_label="$1000", comments=0)
    contested = base.with_linked_prs(2)
    assert scoring.score(contested, now=NOW) < scoring.score(base, now=NOW)
    # 1000 / (1 + 0 + 2*2) = 200, against 1000 / 1 = 1000, both at health 1.0
    assert scoring.score(contested, now=NOW) == pytest.approx(200.0)


def test_comments_and_open_prs_both_count():
    cand = _cand(amount_label="$1000", comments=3).with_linked_prs(1)
    # 1000 / (1 + 3 + 2) = 166.67
    assert scoring.score(cand, now=NOW) == pytest.approx(1000 / 6)


def test_an_unchecked_candidate_scores_as_if_uncontested():
    # open_pr_count is None when stage 3 never ran -- the core budget can run
    # out mid-run. Guessing a penalty would be inventing evidence about a check
    # we could not afford to make.
    base = _cand(amount_label="$1000", comments=0)
    assert base.open_pr_count is None
    assert scoring.score(base, now=NOW) == pytest.approx(1000.0)
