"""Metadata-only ranking. No LLM, by design.

    score = amount_usd / (1 + comments + 2 x open_prs) x repo_health x farm_penalty

`comments` is the competition proxy and arrives free in the search payload, so
it costs no extra call. repo_health discounts repos where correct work is still
likely to go unmerged or unpaid. farm_penalty sinks the shape that survives
payers.payout_reject -- a young, unstarred repo with a huge open backlog that
has happened to close a bounty once.

The bands below are a starting point, not a measured optimum. Revisit them once
the digest has produced enough real outcomes to calibrate against.

Age is deliberately absent. Measured 2026-08-05: 1 of 521 live candidates was
created in the previous 30 days, so an age term discriminates nothing -- and
"old with zero comments" means either hard or overlooked, which metadata cannot
separate. first_seen_at in store.py supplies real freshness from run 2 onward.
"""

from .models import Candidate, _now_utc, parse_iso

RECENT_PUSH_DAYS = 90


def repo_health(stars: int, pushed_at: str, *, now: float | None = None) -> float:
    """Multiplier in [0.35, 1.0] -- the product of a stars band and an activity band."""
    if stars >= 500:
        star_factor = 1.0
    elif stars >= 100:
        star_factor = 0.8
    elif stars >= 10:
        star_factor = 0.6
    else:
        star_factor = 0.5

    days_since_push = (_now_utc(now) - parse_iso(pushed_at)).days
    activity_factor = 1.0 if days_since_push <= RECENT_PUSH_DAYS else 0.7

    return star_factor * activity_factor


# Farm shape, calibrated against the one measured example rather than invented.
# zhangjiayang6835-cyber/bounty-plaza on 2026-08-09: created 2026-07-08 (32 days),
# 5 stars, 526 open issues, 0 bounties ever closed. It held three of the top four
# digest slots.
YOUNG_REPO_DAYS = 90
FEW_STARS = 20
HUGE_BACKLOG = 200


def farm_penalty(cand: Candidate, *, now: float | None = None) -> float:
    """Multiplier in {1.0, 0.6, 0.25} on how much this repo smells like a farm.

    A penalty, never a reject. payers.payout_reject already removes the clear
    cases on evidence -- a repo advertising a pile of bounties and never closing
    one. This catches the shape that survives it: a young, unstarred repo with a
    huge open backlog that has happened to close something. Sinking it keeps it
    visible so the operator can judge, which rejecting would not.

    Missing fields score 1.0. They mean enrichment never ran, not that the repo
    is clean, and penalising absent data would punish a failed API call.
    """
    if cand.repo_created_at is None or cand.repo_open_issues is None:
        return 1.0
    if cand.stars is None:
        return 1.0

    if cand.stars >= FEW_STARS:
        # A funded launch can be young with a big backlog of its own filed
        # issues and still be nothing like bounty-plaza (5 stars). Stars are
        # the signal that tells a farm from a generously young project, so
        # without it the other two -- alone or together -- do not sink the
        # score; "young, unstarred, huge backlog" is a conjunction, not a
        # majority vote among three independent flags.
        return 1.0

    age_days = (_now_utc(now) - parse_iso(cand.repo_created_at)).days
    # +1 for few_stars, already established by the early return above.
    signals = 1 + sum((age_days < YOUNG_REPO_DAYS,
                        cand.repo_open_issues > HUGE_BACKLOG))
    if signals == 3:
        return 0.25
    if signals == 2:
        return 0.6
    return 1.0


def score(cand: Candidate, *, now: float | None = None) -> float:
    """Rank this candidate. Raises if repo detail was never fetched.

    An unenriched candidate is a run that failed partway, not a bad bounty --
    scoring it with an invented health value would bury a real failure inside a
    plausible-looking number.
    """
    if cand.stars is None or cand.pushed_at is None:
        raise ValueError(f"{cand.repo}#{cand.number} is not enriched with repo detail")
    if cand.amount_usd is None:
        return 0.0
    # An open PR is a rival who has already written code, so it weighs more than
    # a comment -- but it is not a wall. Expensify selects by proposal and Algora
    # by merge, so a later entrant can read the open PRs and beat them. None
    # means stage 3 never ran, and inventing a penalty for a check we could not
    # afford would be inventing evidence.
    competition = 1 + cand.comments + 2 * (cand.open_pr_count or 0)
    return (cand.amount_usd / competition
            * repo_health(cand.stars, cand.pushed_at, now=now)
            * farm_penalty(cand, now=now))
