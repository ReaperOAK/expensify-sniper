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
label-gated rule never saw it because it wore no Algora label at all. That is
the shape this module reports, and nothing more.
"""

from __future__ import annotations

import json
import sqlite3
import time

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
    closed_count: int | None,
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
      * and it has never once CLOSED a bounty carrying that label.

    The last condition replaces the old requirement that the repo wear Algora's
    exact `💎 Bounty` string. That gate existed for a real reason -- a repo with
    no Algora label has no payout record to be absent -- but it meant any farm
    using its own label was invisible. Measured 2026-08-09,
    zhangjiayang6835-cyber/bounty-plaza held three of the top four digest slots
    with 526 open bounties, 0 ever closed, 5 stars and a month of history, all
    behind a plain "Bounty" label.

    `closed_count` of None means the question could not be answered, and this
    abstains. A repo running its own programme, or a small repo with one or two
    genuine bounties, is never touched.
    """
    if not payers:
        return None  # roster unavailable: abstain rather than reject the world
    if repo in payers:
        return None
    if bounty_label_of(labels) is None:
        return None  # nothing marks this as a bounty; its silence says nothing
    if open_bounties_here < config.UNPAID_REPO_THRESHOLD:
        return None
    if closed_count is None or closed_count > 0:
        return None
    return (f"no payout history ({open_bounties_here} open bounties here, "
            f"0 ever closed)")


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


def ever_closed_with(
    conn: sqlite3.Connection,
    repo: str,
    label: str,
    *,
    now: float | None = None,
) -> int | None:
    """How many issues `repo` has ever CLOSED carrying `label`. None = unknown.

    This is the evidence that replaces "is it wearing Algora's label". A repo
    advertising a pile of open bounties and never closing one is a farm whatever
    label it uses: measured 2026-08-09, zhangjiayang6835-cyber/bounty-plaza had
    526 open, 0 ever closed, 5 stars, and a month of history, while holding three
    of the top four digest slots.

    Closed rather than rewarded, deliberately. `💰 Rewarded` is Algora's marker
    and a repo running its own programme never sets it -- commaai/opendbc has
    closed seven bounties including a $4,000 port with no Algora label anywhere.
    Closing a bounty is the weakest claim that still separates a working
    programme from a wall of adverts.

    Costs one search unit, cached for config.PAYER_CACHE_S. Returns None on any
    failure, and the caller MUST abstain rather than read that as zero.
    """
    stamp = time.time() if now is None else now
    key = f"{CLOSED_CACHE_PREFIX}:{repo}:{label}"
    _, body = store.get_cache(conn, key)
    if body:
        try:
            payload = json.loads(body)
            if stamp - payload["fetched_at"] < config.PAYER_CACHE_S:
                return payload["closed"]
        except (ValueError, KeyError, TypeError):
            pass  # unreadable cache is just a miss

    if not store.reserve_or_wait(conn, "search", 1):
        return None
    try:
        found = github_api.search_issues(
            f'repo:{repo} is:issue is:closed label:"{label}"',
            per_page=1, max_pages=1)
    except Exception:  # noqa: BLE001 -- unknown, and the caller abstains on it
        return None

    store.set_cache(conn, key, None,
                    json.dumps({"fetched_at": stamp, "closed": found.total_count}))
    return found.total_count
