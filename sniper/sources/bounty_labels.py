"""Discovery over GitHub bounty labels -- the backbone source.

This reaches Algora's inventory for free: Algora applies its `💎 Bounty` label
to the real GitHub issue, so its bounties are mirrored into GitHub by design.
Algora's own API is not viable (its REST endpoint returns
{"errors":{"detail":"Not Acceptable"}} and its /bounties page is a Phoenix
LiveView shell with no amounts in the served HTML), and Polar sunset bounties
entirely. GitHub Search is the only live source.

sort=updated, never created: bounty labels land on issues that already exist.
Measured 2026-08-05, 1 of 521 live candidates was created in the previous 30
days, so `created` ordering would miss essentially everything. Incremental
discovery therefore comes from diffing updated_at against SQLite -- search
responses carry no ETag, so there is nothing to revalidate.
"""

from __future__ import annotations

import sqlite3

from .. import config, github_api, store
from ..models import Candidate
from .base import SourceResult


class BountyLabelSource:
    name = "github-labels"

    def __init__(self, queries: list[str] | None = None, *, max_pages: int | None = None):
        self.queries = config.BOUNTY_QUERIES if queries is None else queries
        self.max_pages = config.BOUNTY_SEARCH_MAX_PAGES if max_pages is None else max_pages

    def discover(self, conn: sqlite3.Connection) -> SourceResult:
        result = SourceResult()
        by_key: dict[tuple[str, str, int], Candidate] = {}

        for query in self.queries:
            if not store.reserve_or_wait(conn, "search", self.max_pages):
                result.warnings.append(
                    f"search budget exhausted before query {query!r} -- not run")
                continue
            try:
                found = github_api.search_issues(
                    query, sort="updated", order="desc",
                    per_page=100, max_pages=self.max_pages,
                    stop=self._page_is_all_known(conn),
                )
            except github_api.RateLimited as exc:
                result.warnings.append(f"query {query!r} rate limited: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 -- one bad query must not kill the run
                result.warnings.append(f"query {query!r} failed: {exc}")
                continue

            if found.incomplete:
                result.warnings.append(
                    f"query {query!r} returned incomplete results "
                    f"({len(found.items)} of {found.total_count}) -- GitHub timed out")

            for item in found.items:
                try:
                    cand = Candidate.from_search_item(self.name, item)
                except Exception as exc:  # noqa: BLE001 -- one malformed item
                    # must not discard every candidate already collected. Only
                    # the network call was guarded before, so a missing
                    # repository_url took the whole run down with no warning.
                    result.warnings.append(
                        f"query {query!r}: skipped an unusable result item: {exc}")
                    continue
                by_key[cand.key] = cand

        result.candidates = list(by_key.values())
        return result

    def _page_is_all_known(self, conn: sqlite3.Connection):
        """Stop paging once an entire page is already recorded at this updated_at.

        Results are newest-updated first, so a fully-known page means everything
        beyond it is older still. On run 1 nothing is known, so this never fires
        and the backlog is pulled in full -- which is the intended run-1 behavior.
        """

        def stop(items: list[dict]) -> bool:
            for item in items:
                try:
                    cand = Candidate.from_search_item(self.name, item)
                except Exception:  # noqa: BLE001 -- an item we cannot even key is
                    # by definition not one we have already recorded, so keep
                    # paging. Raising here would abort discovery from inside a
                    # callback, where there is no warnings list to report into.
                    return False
                if store.known_updated_at(conn, *cand.key) != cand.updated_at:
                    return False
            return True

        return stop
