"""Expensify Help Wanted, wrapped as one Source among several.

The existing watcher polls /issues/events rather than /issues because
`Help Wanted` is applied to issues that already exist -- the same trap that
makes bounty-label discovery sort by `updated`. That poll is reused as-is; this
module only reshapes its output into Candidates.

Help Wanted pays a flat rate (~$250), so there is no $-label to parse and
amount_usd comes from config. verify.py is untouched: it owns proposal drafting
for these issues and this source only surfaces them for ranking.
"""

from __future__ import annotations

import sqlite3

from .. import config, github_api, store
from ..models import Candidate
from .base import SourceResult


class ExpensifySource:
    name = "expensify"
    CACHE_KEY = "expensify-help-wanted-events"

    def __init__(self, repo: str | None = None):
        self.repo = config.REPO if repo is None else repo

    def discover(self, conn: sqlite3.Connection) -> SourceResult:
        result = SourceResult()

        if not store.reserve_or_wait(conn, "core", 1):
            result.warnings.append("core budget exhausted -- Expensify poll skipped")
            return result

        etag, _ = store.get_cache(conn, self.CACHE_KEY)
        try:
            events, new_etag, changed = github_api.poll_help_wanted_events(self.repo, etag)
        except github_api.RateLimited as exc:
            result.warnings.append(f"Expensify event poll rate limited: {exc}")
            return result
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"Expensify event poll failed: {exc}")
            return result

        if not changed:
            return result
        store.set_cache(conn, self.CACHE_KEY, new_etag, "")

        for event in events:
            # Check-then-mark, deliberately not mark-then-work. mark_event_seen()
            # commits, so marking before the fetch burns an event we never managed
            # to fetch: the next run skips it as "already handled". A warning
            # makes that visible but not recoverable. Marking only once a
            # candidate exists costs at most one repeated core call per run for a
            # persistently broken issue, against a 5000/hr budget -- far cheaper
            # than silently dropping a $250 bounty.
            if store.event_seen(conn, event["event_id"]):
                continue  # already handled on an earlier run
            if not store.reserve_or_wait(conn, "core", 1):
                result.warnings.append(
                    f"core budget exhausted -- Expensify #{event['issue']} not "
                    f"fetched; it stays unseen and will be retried next run")
                break
            try:
                issue = github_api.get_issue(self.repo, event["issue"])
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(
                    f"Expensify #{event['issue']} fetch failed: {exc}")
                continue
            try:
                candidate = Candidate.from_issue(
                    self.name, self.repo, issue,
                    amount_usd=config.EXPENSIFY_BOUNTY_USD)
            except Exception as exc:  # noqa: BLE001 -- a malformed payload must
                # not discard candidates already collected in this run.
                result.warnings.append(
                    f"Expensify #{event['issue']} payload unusable: {exc}")
                continue
            store.mark_event_seen(conn, event["event_id"], event["issue"],
                                  event["labeled_at"])
            result.candidates.append(candidate)

        return result
