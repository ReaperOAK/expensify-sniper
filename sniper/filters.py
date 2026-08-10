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
from .models import Candidate, _now_utc, parse_iso


def stale_reason(days: int | None = None) -> str:
    """The one spelling of the staleness verdict, for both paths that emit it.

    It used to interpolate the MEASURED idle count -- "no activity in 221d" --
    so every candidate produced a different spelling of the same rule. The
    board groups its reject table by the exact string and clear_rejections
    matches it exactly, so that turned one big rule into dozens of one-row
    reasons nothing could see the size of or reverse. It also made it
    impossible for store.apply_staleness to write anything free_reject would
    agree with, since the retroactive pass judges in SQL and has no per-row
    idle count to interpolate.

    The threshold, not the measurement: "over 7d" is the whole content of the
    verdict, and 221 was decoration.
    """
    return f"no activity in over {config.STALE_AFTER_DAYS if days is None else days}d"


def free_reject(
    cand: Candidate,
    *,
    now: float | None = None,
    paid_labels: set[str] | None = None,
    floor: int | None = None,
    trusted: bool = False,
) -> str | None:
    """Reason to drop this candidate, from the search payload alone. None = keep.

    Sources listed in config.ASSIGNEE_NOT_A_CLAIM are exempt from the assignee rule.
    """
    # Normalise the override too, not just config's. The contract is
    # case-insensitive matching, and a caller passing {"Paid Out"} would
    # otherwise silently match nothing.
    paid = {label.strip().lower()
            for label in (config.PAID_LABELS if paid_labels is None else paid_labels)}
    minimum = config.AMOUNT_FLOOR_USD if floor is None else floor

    if cand.state != "open":
        return "closed"
    # An assignee is a claim on most sources and pure noise on Expensify, which
    # assigns an internal engineer at posting time. See config.ASSIGNEE_NOT_A_CLAIM.
    # Those sources are judged by stage 3's claim test instead, which is the
    # sharper question everywhere: is an assignee also the author of an open PR?
    # Lowered on both sides: config normalises its entries, so a source named
    # "Expensify" must not miss an exemption spelled "expensify".
    if cand.has_assignee and cand.source.lower() not in config.ASSIGNEE_NOT_A_CLAIM:
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
    # Comments are the competition proxy everywhere else; past the ceiling they
    # are a verdict. See config.COMMENT_CEILING for the measurement, and for
    # what this costs -- it is not free, and the comment says which real
    # candidate it drops.
    if cand.comments > config.COMMENT_CEILING:
        return f"{cand.comments} comments -- too contested"
    # Silence, measured on last activity. NOT on created_at -- see
    # config.STALE_AFTER_DAYS for why that would have emptied the board.
    idle = (_now_utc(now) - parse_iso(cand.updated_at)).days
    if idle > config.STALE_AFTER_DAYS:
        return stale_reason()
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
