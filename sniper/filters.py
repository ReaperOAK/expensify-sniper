"""Rejection rules, ordered by what they cost to evaluate.

free_reject() is decided from the search payload alone. repo_reject() needs the
repo-detail enrichment call. Running them in that order is the whole reason
quota is never spent on garbage.

The split is forced by the API, not by taste: search result items carry only
`repository_url` -- no fork, archived, stars or pushed_at (measured
2026-08-05) -- so those flags simply are not available for free.

Both return a reason string rather than a bool. A rejected candidate is written
to the store with its reason, so a run can always answer "why did this drop
out" instead of leaving a hole.
"""

from . import config
from .models import Candidate


def free_reject(
    cand: Candidate,
    *,
    paid_labels: set[str] | None = None,
    floor: int | None = None,
    trusted: bool = False,
) -> str | None:
    """Reason to drop this candidate, from the search payload alone. None = keep."""
    # Normalise the override too, not just config's. The contract is
    # case-insensitive matching, and a caller passing {"Paid Out"} would
    # otherwise silently match nothing.
    paid = {label.strip().lower()
            for label in (config.PAID_LABELS if paid_labels is None else paid_labels)}
    minimum = config.AMOUNT_FLOOR_USD if floor is None else floor

    if cand.state != "open":
        return "closed"
    if cand.has_assignee:
        return "assigned"
    if any(label.strip().lower() in paid for label in cand.labels):
        return "already rewarded"
    # A real bounty carries one amount. Three at once is a spam farm --
    # SecureBananaLabs/bug-bounty#11398 carries $780, $1k and $1.2k. Sharpest
    # spam signal in the data.
    #
    # Unless the repo has actually paid people. go-gitea/gitea#1872 carries $250,
    # $50 and $500: tiers for different scopes, on a repo with a real payout
    # history. The rule cannot tell tiers from spam on its own, so payout history
    # decides -- and getting this wrong hid a live bounty at a proven payer.
    if len(cand.amounts_seen) > 1 and not trusted:
        return "multiple amount labels"
    # A missing amount is not a reject: it sorts last, because expected value
    # cannot be computed without one.
    if cand.amount_usd is not None and cand.amount_usd < minimum:
        return f"below ${minimum} floor"
    return None


def repo_reject(cand: Candidate) -> str | None:
    """Reason to drop this candidate given repo detail. None = keep.

    Raises if the candidate was never enriched, rather than silently keeping it:
    "unknown" and "fine" are different answers.
    """
    if cand.fork is None or cand.archived is None:
        raise ValueError(f"{cand.repo}#{cand.number} is not enriched with repo detail")
    if cand.fork:
        return "fork"
    if cand.archived:
        return "archived repo"
    return None
