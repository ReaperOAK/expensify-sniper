"""Metadata-only ranking. No LLM, by design.

    score = amount_usd / (1 + comments) x repo_health

`comments` is the competition proxy and arrives free in the search payload, so
it costs no extra call. repo_health discounts repos where correct work is still
likely to go unmerged or unpaid.

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
    health = repo_health(cand.stars, cand.pushed_at, now=now)
    return cand.amount_usd / (1 + cand.comments) * health
