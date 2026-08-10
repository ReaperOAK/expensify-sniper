"""Which repositories have ever actually paid a bounty.

This is the signal the tracker was missing, and it is worth more than every
other ranking term combined. Measured 2026-08-09 against live inventory:

    repo                        open bounties   ever rewarded   ever closed
    ClankerNation/OpenAgents              201               0             0
    UnsafeLabs/Bounty-Hunters             182               0             0
    activepieces/activepieces               1             150           173

The first two advertised $917,000 and top-scored the digest on amount alone.
Between them they have never paid anyone, and never even closed a bounty issue.
The third, offering $200, is a proven payer. Expected value ranks them exactly
backwards unless payout history is part of it.

Algora stamps `💰 Rewarded` on an issue when it releases the money, so one
search returns a roster of repos known to pay. Absence from that roster is NOT
evidence of non-payment on its own -- commaai/opendbc runs its own bounty
programme, uses no Algora label, and has closed seven bounties including a
$4,000 port.

Absence only becomes damning when direct evidence backs it up: the repo is
advertising a pile of open bounties under some label it claims marks a bounty,
and has never once CLOSED an issue carrying that label. Checked directly via
`ever_closed_with`, not gated on whether the repo happens to wear Algora's own
string -- gating on that string was the earlier version of this rule, and it
was blind to a farm running its own programme. Measured 2026-08-09,
zhangjiayang6835-cyber/bounty-plaza held three of the top four digest slots
with 526 open bounties behind a plain "Bounty" label and 0 ever closed; the
label-gated rule never saw it because it wore no Algora label at all.

Closing is not paying, though, and by 2026-08-10 the same farm had learned
that: 527 open, but now 198 closed under its own "Bounty" label -- enough to
sail past the ">0 ever closed" rule above and hold the top five digest slots
again. Sampling #12, #13 and #16 found them closed 2026-07-12T10:43:51Z /
:55Z / :58Z -- seven seconds apart, none assigned, one titled "[Bounty]
[bounty][funding-pending]: complete the first permi...". A bulk close, not
payout history: a repo can close its own issues as freely as it opens them. A
real programme closes an issue BECAUSE work merged, so its closures carry
linked pull requests. `ever_closed_with` now reports both counts, and
`payout_reject` rejects on a low ratio the same way it rejects on zero --
bounty-plaza's ratio is 23/198 (12%), against activepieces/activepieces'
170/173 (98%) and commaai/opendbc's 4/7 (57%). See
`config.CLOSED_PR_RATIO_FLOOR` for the measurement that set the line.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from . import config, github_api, store
from .models import parse_amount_label, repo_from_url

CACHE_KEY = "known-payer-repos"

# Algora's paid marker, and the label a farm has to be wearing for its silence
# to mean anything.
REWARDED_LABEL = "💰 Rewarded"
ALGORA_LABEL = "💎 Bounty"


def known_payers(
    conn: sqlite3.Connection,
    *,
    max_pages: int | None = None,
    now: float | None = None,
) -> tuple[set[str], list[str]]:
    """Return (repos seen paying an Algora bounty, warnings).

    Cached in `http_cache` for `config.PAYER_CACHE_S`: payout history moves in
    days, not in the three hours between runs, and this costs scarce search
    quota. On a cache hit no request is made at all.

    A failure here returns an EMPTY set and a warning rather than raising: an
    empty roster makes the payout filter abstain, which is the safe direction.
    Silently returning an empty set with no warning would instead look like
    "nobody has ever paid" and reject the entire world.
    """
    stamp = time.time() if now is None else now
    cached_at, body = store.get_cache(conn, CACHE_KEY)
    if body:
        try:
            payload = json.loads(body)
            if stamp - payload["fetched_at"] < config.PAYER_CACHE_S:
                return set(payload["repos"]), []
        except (ValueError, KeyError, TypeError):
            pass  # unreadable cache is just a miss

    pages = config.PAYER_SEARCH_MAX_PAGES if max_pages is None else max_pages
    if not store.reserve_or_wait(conn, "search", pages):
        return set(), ["search budget exhausted -- payout history not checked, "
                       "so no repo was judged on whether it has ever paid"]
    try:
        found = github_api.search_issues(
            f'label:"{REWARDED_LABEL}"', sort="updated", order="desc",
            per_page=config.SEARCH_PER_PAGE, max_pages=pages,
        )
    except Exception as exc:  # noqa: BLE001
        return set(), [f"payout history lookup failed ({exc}) -- no repo was "
                       f"judged on whether it has ever paid"]

    repos = set()
    for item in found.items:
        try:
            repos.add(repo_from_url(item["repository_url"]))
        except Exception:  # noqa: BLE001 - one malformed item must not lose the rest
            continue

    warnings = []
    if found.incomplete:
        warnings.append(
            f"payout history is partial ({len(found.items)} of {found.total_count} "
            f"rewarded issues seen) -- a repo may look unpaid only because we "
            f"could not see its payout")

    store.set_cache(conn, CACHE_KEY, cached_at,
                    json.dumps({"fetched_at": stamp, "repos": sorted(repos)}))
    return repos, warnings


def payout_reject(
    repo: str,
    labels: tuple[str, ...],
    open_bounties_here: int,
    payers: set[str],
    closed: ClosedBounties | None,
) -> str | None:
    """Reason to drop a candidate on payout history. None = keep.

    Still deliberately narrow. It fires only when all of these hold:

      * the payer roster was actually fetched (an empty one means the lookup
        failed, not that nobody pays);
      * the repo has never appeared in that roster;
      * the candidate carries some label marking it as a bounty, so the repo's
        silence is about something it actually claims to run;
      * it is advertising at least `config.UNPAID_REPO_THRESHOLD` open bounties,
        which is a farm's shape rather than a new project's;
      * and either it has never once CLOSED a bounty carrying that label, OR it
        has closed some but too few of them carry a linked pull request.

    The "never closed" condition replaces the old requirement that the repo
    wear Algora's exact `💎 Bounty` string. That gate existed for a real reason
    -- a repo with no Algora label has no payout record to be absent -- but it
    meant any farm using its own label was invisible. Measured 2026-08-09,
    zhangjiayang6835-cyber/bounty-plaza held three of the top four digest slots
    with 526 open bounties, 0 ever closed, 5 stars and a month of history, all
    behind a plain "Bounty" label.

    The PR-ratio condition exists because ">0 ever closed" alone stopped being
    enough: by 2026-08-10 that same farm had bulk-closed its way to 198 closed
    issues under its own label, only 23 of them (12%) carrying any linked PR --
    three sampled closures landed seven seconds apart, none assigned, one
    titled "...[bounty][funding-pending]:...". A repo closes an issue BECAUSE
    work merged if it is running a real programme, so its closures should carry
    linked PRs: activepieces/activepieces is 170/173 (98%), commaai/opendbc --
    no Algora label, its own programme -- is 4/7 (57%). Both clear
    `config.CLOSED_PR_RATIO_FLOOR` (0.34) comfortably; the farm does not.

    `closed` of None means the question could not be answered, and this
    abstains. A repo running its own programme, or a small repo with one or two
    genuine bounties, is never touched.

    The first four conditions live in `label_to_check`, not here, because every
    caller has to evaluate them BEFORE calling this -- they decide whether
    spending TWO search units on `closed` can possibly change the answer.
    Two copies of that reasoning is the shape of the bug fixed in 441d2ed, and
    the risk is asymmetric: a copy that drifts more PERMISSIVE only wastes
    quota, since this function re-validates, while one that drifts more
    RESTRICTIVE passes closed=None where real evidence was needed and
    silently keeps a farm.
    """
    if label_to_check(repo, labels, open_bounties_here, payers) is None:
        return None
    if closed is None:
        return None
    if closed.total == 0:
        return (f"no payout history ({open_bounties_here} open bounties here, "
                f"0 ever closed)")
    # total > 0 is checked above precisely so this division never sees a zero
    # denominator -- a repo with 0 closed never reaches this line.
    if (closed.with_pr / closed.total) < config.CLOSED_PR_RATIO_FLOOR:
        return (f"no payout history ({closed.total} closed here, only "
                f"{closed.with_pr} with any linked PR)")
    return None


def label_to_check(
    repo: str,
    labels: tuple[str, ...],
    open_bounties_here: int,
    payers: set[str],
) -> str | None:
    """The label to ask `ever_closed_with` about, or None to spend nothing.

    This is `payout_reject`'s gate, hoisted so a caller can ask it BEFORE
    buying the evidence. None means the answer cannot change the verdict:

      * the payer roster is empty, so the lookup FAILED and payout_reject's
        first check abstains unconditionally -- this one bit us, and asking
        anyway spends TWO search units per candidate on an answer already
        guaranteed to be discarded. Worse than wasted: once the 30/min pool is
        gone, reserve_or_wait blocks for up to BUDGET_WAIT_S, and the roster
        fetch usually fails from that same rate pressure;
      * the repo is already a proven payer;
      * nothing on the candidate marks it as a bounty, so the repo's silence
        says nothing;
      * the repo is under UNPAID_REPO_THRESHOLD open bounties, which is a new
        project rather than a farm.

    Returning the label rather than a bool means the caller never recomputes
    bounty_label_of, so the two can never disagree about WHICH label was judged.
    """
    if not payers:
        return None  # roster unavailable: abstain rather than reject the world
    if repo in payers:
        return None
    label = bounty_label_of(labels)
    if label is None:
        return None  # nothing marks this as a bounty; its silence says nothing
    if open_bounties_here < config.UNPAID_REPO_THRESHOLD:
        return None
    return label


def bounty_label_of(labels: tuple[str, ...]) -> str | None:
    """The label to test this repo's payout history against, or None to abstain.

    Preference order is evidence order. Algora's own label is unambiguous.
    Anything containing "bounty" is a self-declared bounty programme, which is
    what bounty-plaza wears -- 526 open, 0 ever closed, invisible to a rule that
    demanded Algora's exact string. An amount label is the weakest of the three
    and is only reached when the other two are absent.

    None means the candidate carries nothing marking it as a bounty at all, and
    the caller must abstain: a repo's silence about a label it does not use says
    nothing about whether it pays.

    Every branch returns the STRIPPED label, not the raw one: the return value
    flows straight into `ever_closed_with`'s query as `label:"{label}"`, where
    incidental leading/trailing whitespace would silently fail to match a real
    closed issue.
    """
    for label in labels:
        if label.strip() == ALGORA_LABEL:
            return label.strip()
    for label in labels:
        if "bounty" in label.lower():
            return label.strip()
    for label in labels:
        if parse_amount_label(label) is not None:
            return label.strip()
    return None


CLOSED_CACHE_PREFIX = "closed-bounty"


@dataclass(frozen=True)
class ClosedBounties:
    """How many `label`-carrying issues a repo has ever CLOSED, and how many of
    those closures carry a linked pull request.

    Two numbers, not one, because "ever closed" alone stopped being evidence of
    payout the moment a farm started bulk-closing: measured 2026-08-10,
    zhangjiayang6835-cyber/bounty-plaza reported 198 closed under its own
    "Bounty" label, but only 23 (12%) with any linked PR -- three sampled
    closures (#12, #13, #16) landed seven seconds apart, none assigned. See
    `config.CLOSED_PR_RATIO_FLOOR` for the full measured table and the line it
    sets between that and a real programme (activepieces 98%, opendbc 57%).
    """

    total: int
    with_pr: int


def ever_closed_with(
    conn: sqlite3.Connection,
    repo: str,
    label: str,
    *,
    now: float | None = None,
    sleep=time.sleep,
) -> ClosedBounties | None:
    """How `repo` has ever CLOSED issues carrying `label`. None = unknown.

    This is the evidence that replaces "is it wearing Algora's label". A repo
    advertising a pile of open bounties and never closing one is a farm whatever
    label it uses: measured 2026-08-09, zhangjiayang6835-cyber/bounty-plaza had
    526 open, 0 ever closed, 5 stars, and a month of history, while holding three
    of the top four digest slots.

    Closed rather than rewarded, deliberately. `💰 Rewarded` is Algora's marker
    and a repo running its own programme never sets it -- commaai/opendbc has
    closed seven bounties including a $4,000 port with no Algora label anywhere.

    But closed alone was not enough either: by 2026-08-10 the same farm had
    grown to 198 closed under its own label -- enough to survive the old
    ">0 ever closed" rule -- with only 23 carrying any linked PR. Closing an
    issue is free; closing it BECAUSE a pull request merged is not, so this
    also asks `linked:pr` of the exact same query. `-no:assignee` was tried and
    dropped: measured 2026-08-10 it returned the identical 198 for this farm,
    whose issues have no assignee, because GitHub's search API silently ignores
    it. `linked:pr` does discriminate.

    Costs TWO search units -- one per query -- cached together under one key
    for config.PAYER_CACHE_S. Returns None on any failure in EITHER query, and
    the caller MUST abstain rather than read that as zero. A failure is NOT
    written to the cache -- it is not an answer about the repo, and caching a
    partial answer (total known, with_pr not) would silently read back as a
    100%-unlinked repo -- so every caller in a loop must memoise the None
    itself, or one throttle is re-paid once per candidate. hunt.run and
    store.apply_payout_rejections both do.
    """
    stamp = time.time() if now is None else now
    key = f"{CLOSED_CACHE_PREFIX}:{repo}:{label}"
    _, body = store.get_cache(conn, key)
    if body:
        try:
            payload = json.loads(body)
            if stamp - payload["fetched_at"] < config.PAYER_CACHE_S:
                return ClosedBounties(payload["closed"], payload["closed_with_pr"])
        except (ValueError, KeyError, TypeError):
            pass  # unreadable cache is just a miss

    # Reserved together, not one-then-one: there is no use for the first
    # search's answer without the second, so a partial reservation would only
    # let one request out before discovering the budget cannot cover the pair.
    if not store.reserve_or_wait(conn, "search", 2):
        return None

    # Pace EACH request like a search PAGE, because to GitHub's burst guard
    # that is exactly what it is. search_issues only pauses between pages of
    # ONE query and this fires two separate one-page queries back to back, so
    # without pacing both, the second one alone reproduces the burst pattern
    # measured 2026-08-08: TEN back-to-back requests tripped the secondary
    # limit with 23 of 30 primary units still unspent. apply_payout_rejections
    # fires this once per qualifying repo, and reviving the 446 'open linked
    # PR' rows takes qualifying repos from 4 to 14 -- the same pressure, twice
    # per repo now instead of once.
    if config.SEARCH_PAGE_PAUSE_S:
        sleep(config.SEARCH_PAGE_PAUSE_S)
    try:
        total_found = github_api.search_issues(
            f'repo:{repo} is:issue is:closed label:"{label}"',
            per_page=1, max_pages=1,
            # No retry ladder here, unlike discovery. The default is 3 attempts
            # at 60/120/240s, which exists because abandoning a DISCOVERY page
            # cost 420 of 520 candidates -- a whole run's inventory. Abandoning
            # a FARM CHECK costs one abstain on one repo, and the caller is
            # built to abstain. Paying 420 seconds for that, per repo, with 14
            # repos qualifying, is how a 3-hourly sweep stops finishing at all.
            retries=0)
    except Exception:  # noqa: BLE001 -- unknown, and the caller abstains on it
        return None

    if config.SEARCH_PAGE_PAUSE_S:
        sleep(config.SEARCH_PAGE_PAUSE_S)
    try:
        pr_found = github_api.search_issues(
            f'repo:{repo} is:issue is:closed label:"{label}" linked:pr',
            per_page=1, max_pages=1, retries=0)
    except Exception:  # noqa: BLE001 -- same as above: unknown, caller abstains
        return None

    result = ClosedBounties(total_found.total_count, pr_found.total_count)
    store.set_cache(conn, key, None,
                    json.dumps({"fetched_at": stamp, "closed": result.total,
                                "closed_with_pr": result.with_pr}))
    return result
