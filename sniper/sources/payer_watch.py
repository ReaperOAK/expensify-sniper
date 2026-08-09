"""Watch the repos that actually pay, and catch a bounty the moment it is posted.

The backlog digest cannot win a real bounty, and the numbers say why. Measured
2026-08-09: Algora repos rewarded 501 issues in ninety days, yet across the ten
most-recently-paying repos there were **three** open unassigned bounties in
total. Expensify is the same shape -- 86 open Help Wanted, zero unassigned,
6,264 closed all time. Anything real is claimed within hours, so what a
three-hourly search still finds is, by construction, what nobody wanted: farms.

Visible inventory is inversely correlated with inventory that pays. The only way
to reach the money is to stop searching for standing stock and start watching
for the moment a label lands -- which is exactly what the Expensify watcher has
always done for one repo.

`/issues/events` is the cheapest watch GitHub offers, and cheap is what makes
this viable across dozens of repos:

  * it draws on the CORE pool (5000/hr), not search (30/min);
  * it supports ETags, and measured 2026-08-09 a 304 left X-RateLimit-Remaining
    completely unchanged -- watching a quiet repo is free;
  * a label lands on an issue that already exists, so `created` ordering would
    never surface it and only the events feed will.

The cost is bounded by repos-that-changed, not repos-watched. Thirty-eight repos
polled every ten minutes is ~228 requests an hour before ETags, and far less
after.

One hard limit: a single page holds 100 events, and on go-gitea/gitea that
covered a SINGLE DAY. Poll slower than a repo's event velocity and labels roll
off the page unseen -- silently, which is why `stale_page_warning` exists.
"""

from __future__ import annotations

import sqlite3

from .. import config, github_api, payers, store
from ..models import Candidate, parse_amount_label
from .base import SourceResult

CACHE_PREFIX = "payer-watch-etag:"


def looks_like_a_bounty_label(name: str) -> bool:
    """Is this label the one that turns an issue into money?

    Three shapes, all observed live: Algora's own marker, a bare amount label
    like `$780`, and the plain word used by repos running their own programme.
    """
    stripped = name.strip()
    if stripped == payers.ALGORA_LABEL:
        return True
    if parse_amount_label(stripped) is not None:
        return True
    return "bounty" in stripped.lower()


class PayerWatchSource:
    """New bounty labels on repos with a proven payout history."""

    name = "payer-watch"

    def __init__(self, repos: list[str] | None = None, *,
                 max_repos: int | None = None):
        self._repos = repos
        self.max_repos = config.WATCH_MAX_REPOS if max_repos is None else max_repos

    def _roster(self, conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
        if self._repos is not None:
            return list(self._repos), []
        known, warnings = payers.known_payers(conn)
        return sorted(known)[: self.max_repos], warnings

    def discover(self, conn: sqlite3.Connection) -> SourceResult:
        result = SourceResult()
        repos, warnings = self._roster(conn)
        result.warnings.extend(warnings)
        if not repos:
            result.warnings.append(
                "payer roster empty -- watching nothing. This is a failure to "
                "look, not a finding that no repo pays.")
            return result

        for repo in repos:
            if not store.reserve_or_wait(conn, "core", 1):
                result.warnings.append(
                    f"core budget exhausted -- stopped watching at {repo}; "
                    f"{len(repos) - repos.index(repo)} payer repos unchecked")
                break
            key = CACHE_PREFIX + repo
            etag, _ = store.get_cache(conn, key)
            try:
                events, new_etag, changed = github_api.poll_label_events(
                    repo, etag, looks_like_a_bounty_label)
            except github_api.RateLimited as exc:
                result.warnings.append(f"watch {repo} rate limited: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 -- one dead repo must not end
                # the sweep; the others may be where the money is.
                result.warnings.append(f"watch {repo} failed: {exc}")
                continue

            if not changed:
                continue  # 304, and it cost nothing
            store.set_cache(conn, key, new_etag, "")

            for event in events:
                if store.event_seen(conn, event["event_id"]):
                    continue
                if not store.reserve_or_wait(conn, "core", 1):
                    result.warnings.append(
                        f"core budget exhausted -- {repo}#{event['issue']} not "
                        f"fetched; it stays unseen and will be retried")
                    break
                try:
                    issue = github_api.get_issue(repo, event["issue"])
                except Exception as exc:  # noqa: BLE001
                    result.warnings.append(
                        f"{repo}#{event['issue']} fetch failed: {exc}")
                    continue
                try:
                    candidate = Candidate.from_issue(self.name, repo, issue)
                except Exception as exc:  # noqa: BLE001
                    result.warnings.append(
                        f"{repo}#{event['issue']} payload unusable: {exc}")
                    continue
                # Marked only once a candidate exists, so a failure above is
                # retried rather than silently burnt.
                store.mark_event_seen(conn, event["event_id"], event["issue"],
                                      event["labeled_at"])
                result.candidates.append(candidate)

        return result
