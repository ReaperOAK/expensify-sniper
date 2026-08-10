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
    # Highest event id already paged past. See poll_label_events: this is
    # what lets a busy hour page deeper instead of losing a label.
    WATERMARK_KEY = "expensify-events-watermark"

    def __init__(self, repo: str | None = None):
        self.repo = config.REPO if repo is None else repo

    def discover(self, conn: sqlite3.Connection) -> SourceResult:
        result = SourceResult()

        # First, and deliberately before the event poll: that poll returns early
        # on a 304, and the standing list has nothing to do with whether the
        # event feed moved. Ordering it after the 304 would have made the whole
        # standing inventory visible only on sweeps where something else changed.
        result.candidates.extend(self._standing(conn, result))

        if not store.reserve_or_wait(conn, "core", 1):
            result.warnings.append("core budget exhausted -- Expensify poll skipped")
            return result

        etag, _ = store.get_cache(conn, self.CACHE_KEY)
        try:
            _, watermark = store.get_cache(conn, self.WATERMARK_KEY)
            events, new_etag, changed = github_api.poll_help_wanted_events(
                self.repo, etag,
                since_event_id=int(watermark) if watermark else None)
        except github_api.RateLimited as exc:
            result.warnings.append(f"Expensify event poll rate limited: {exc}")
            return result
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"Expensify event poll failed: {exc}")
            return result

        if not changed:
            return result
        store.set_cache(conn, self.CACHE_KEY, new_etag, "")
        if events:
            newest = max(int(e["event_id"]) for e in events)
            store.set_cache(conn, self.WATERMARK_KEY, None, str(newest))

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

    def _standing(self, conn: sqlite3.Connection, result: SourceResult) -> list[Candidate]:
        """Every Help Wanted issue open RIGHT NOW, not just ones labelled while
        we watched.

        The event poll above is a live wire: it catches a label as it lands and
        catches nothing else. That is the right instrument for a market where
        real work is claimed within hours -- but it means the standing inventory
        is invisible, because an issue labelled before this tracker existed
        generates no event for it to see.

        Measured 2026-08-10: 83 open Help Wanted issues, none of them in the
        database, while the newest label event was three days old. The watch was
        working exactly as designed and finding nothing, which reads identically
        to a channel that is broken.

        One search, once per sweep. Duplicates are free: upsert_candidate is
        keyed on (source, repo, number), so an issue found by both paths is one
        row. Whether any of these are actually AVAILABLE is not decided here --
        stage 3's claim test and the comment ceiling do that, on evidence.
        """
        if not store.reserve_or_wait(conn, "search", 1):
            result.warnings.append(
                "search budget exhausted -- standing Help Wanted issues not listed")
            return []
        try:
            found = github_api.search_issues(
                f'repo:{self.repo} is:issue is:open label:"{config.HELP_WANTED}"',
                per_page=config.SEARCH_PER_PAGE,
                max_pages=config.EXPENSIFY_STANDING_MAX_PAGES)
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"standing Help Wanted search failed: {exc}")
            return []

        if found.incomplete:
            result.warnings.append(
                f"standing Help Wanted list is partial ({len(found.items)} of "
                f"{found.total_count}) -- an open job may be missing")

        candidates = []
        for item in found.items:
            try:
                candidates.append(Candidate.from_search_item(
                    self.name, item, amount_usd=config.EXPENSIFY_BOUNTY_USD))
            except Exception as exc:  # noqa: BLE001 -- one bad item must not
                # discard the rest of the standing inventory.
                result.warnings.append(
                    f"standing Help Wanted item unusable: {exc}")
        return candidates
