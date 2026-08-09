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
$4,000 port. Absence only becomes damning when a repo is *using Algora's own
label* on a pile of open bounties and has never rewarded one. That is the
shape this module reports, and nothing more.
"""

from __future__ import annotations

import json
import sqlite3
import time

from . import config, github_api, store
from .models import repo_from_url

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
) -> str | None:
    """Reason to drop a candidate on payout history. None = keep.

    Deliberately narrow. It fires only when all three hold:

      * the repo is using Algora's own bounty label, so a payout would have been
        stamped and its absence is meaningful;
      * the repo has never appeared in the rewarded roster;
      * it is advertising at least `config.UNPAID_REPO_THRESHOLD` open bounties,
        which is a farm's shape rather than a new project's.

    A repo running its own programme, or a small repo with one or two genuine
    bounties, is never touched by this.
    """
    if not payers:
        return None  # roster unavailable: abstain rather than reject the world
    if repo in payers:
        return None
    if not any(label.strip() == ALGORA_LABEL for label in labels):
        return None  # not an Algora repo; its silence says nothing
    if open_bounties_here < config.UNPAID_REPO_THRESHOLD:
        return None
    return (f"no payout history ({open_bounties_here} open Algora bounties here, "
            f"0 ever rewarded)")
