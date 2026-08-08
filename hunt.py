#!/usr/bin/env python3
"""Discover, rank and digest open unclaimed paid bounties.

    search -> free rejects -> repo detail -> repo rejects -> timeline -> score -> digest

The order is the design. Free rejects are decided from the search payload alone,
so garbage never costs an enrichment call. Repo detail is fetched once per unique
repo and revalidated by ETag between runs (a 304 costs zero core quota). Timeline
runs on survivors only, and deliberately on the 5000/hr core quota rather than
the 30/min search quota.

Nothing is dropped silently. A rejected candidate is stored with its reason; a
source that fails, a query that comes back incomplete, and a repo that cannot be
enriched all become ⚠ lines in the digest. A short list with no explanation is
indistinguishable from "there is nothing out there", which is HANDOFF.md trap #3.

Usage:
    python3 hunt.py                 # run and send the digest
    python3 hunt.py --dry-run       # run and print the digest, send nothing
    python3 hunt.py --sources github-labels
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sniper import config, filters, github_api, notify, score as scoring, store  # noqa: E402
from sniper.models import Candidate  # noqa: E402
from sniper.sources.base import Source, SourceResult  # noqa: E402
from sniper.sources.bounty_labels import BountyLabelSource  # noqa: E402
from sniper.sources.expensify import ExpensifySource  # noqa: E402

SOURCE_CLASSES = {
    BountyLabelSource.name: BountyLabelSource,
    ExpensifySource.name: ExpensifySource,
}


def build_sources(names: list[str]) -> list[Source]:
    built = []
    for name in names:
        cls = SOURCE_CLASSES.get(name)
        if cls is None:
            raise ValueError(f"unknown source {name!r}; known: {sorted(SOURCE_CLASSES)}")
        built.append(cls())
    return built


def _repo_cache_key(repo: str) -> str:
    return f"repo-detail:{repo}"


def enrich(
    conn: sqlite3.Connection, candidates: list[Candidate], warnings: list[str]
) -> list[Candidate]:
    """Attach repo detail, once per unique repo. Returns candidates that survived.

    A repo we could not fetch produces a warning and its candidates are dropped
    rather than scored -- scoring them would need an invented repo_health, which
    buries a real failure inside a plausible number.
    """
    import json

    details: dict[str, dict] = {}
    enriched: list[Candidate] = []

    for repo in sorted({cand.repo for cand in candidates}):
        if not store.reserve_or_wait(conn, "core", 1):
            warnings.append(f"core budget exhausted -- {repo} not enriched")
            continue
        etag, cached = store.get_cache(conn, _repo_cache_key(repo))
        try:
            payload, new_etag, changed = github_api.get_repo(repo, etag)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"repo detail for {repo} failed: {exc}")
            continue
        if not changed:
            if not cached:
                warnings.append(f"repo detail for {repo} was 304 with no cached body")
                continue
            payload = json.loads(cached)
        else:
            store.set_cache(conn, _repo_cache_key(repo), new_etag,
                            json.dumps(payload, ensure_ascii=False))
        details[repo] = payload

    for cand in candidates:
        payload = details.get(cand.repo)
        if payload is None:
            continue  # already warned above
        enriched.append(cand.with_repo_detail(payload))
    return enriched


def run(
    conn: sqlite3.Connection,
    *,
    sources: list[Source] | None = None,
    now: float | None = None,
    dry_run: bool = False,
) -> str:
    stamp = time.time() if now is None else now
    store.prune_budget(conn, now=stamp)

    first_run = store.candidate_count(conn) == 0
    warnings: list[str] = []

    # Refuse to start a run we cannot finish, rather than reporting an empty world.
    if store.spent(conn, "core", now=stamp) + config.CORE_BUDGET_PER_RUN > \
            config.CORE_QUOTA_PER_HOUR:
        warnings.append(
            f"core budget too low to start "
            f"({store.spent(conn, 'core', now=stamp)}/{config.CORE_QUOTA_PER_HOUR} "
            f"spent this hour) -- run skipped")
        text = notify.format_digest([], [], warnings, first_run=False, now=stamp)
        if not dry_run:
            notify.send(text)
        return text

    if sources is None:
        sources = build_sources(config.HUNT_ENABLED_SOURCES)

    discovered: list[Candidate] = []
    for source in sources:
        try:
            result: SourceResult = source.discover(conn)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"source {source.name} failed: {exc}")
            continue
        warnings.extend(result.warnings)
        discovered.extend(result.candidates)

    # Stage 1 -- free rejects. Search payload only; zero API calls.
    survivors: list[Candidate] = []
    for cand in discovered:
        reason = filters.free_reject(cand)
        if reason:
            store.upsert_candidate(conn, cand, rejected=reason, now=stamp)
        else:
            survivors.append(cand)

    # Stage 2 -- repo detail, then repo rejects.
    survivors = enrich(conn, survivors, warnings)
    still: list[Candidate] = []
    for cand in survivors:
        reason = filters.repo_reject(cand)
        if reason:
            store.upsert_candidate(conn, cand, rejected=reason, now=stamp)
        else:
            still.append(cand)

    # Stage 3 -- timeline, survivors only.
    for cand in still:
        if not store.reserve_or_wait(conn, "core", 1):
            warnings.append(
                f"core budget exhausted -- {cand.repo}#{cand.number} timeline not checked")
            break
        try:
            taken = github_api.has_open_linked_pr(cand.repo, cand.number)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"timeline for {cand.repo}#{cand.number} failed: {exc}")
            continue
        if taken:
            store.upsert_candidate(conn, cand, rejected="open linked PR", now=stamp)
            continue
        store.upsert_candidate(conn, cand.with_score(scoring.score(cand, now=stamp)),
                               now=stamp)

    # Stage 4 -- digest.
    if first_run:
        new_rows = store.live_candidates(conn, config.DIGEST_TOP_N)
        standing_rows = []
    else:
        new_rows = store.live_candidates(conn, config.DIGEST_TOP_N, since=stamp)
        standing_rows = store.live_candidates(conn, config.DIGEST_STANDING_N)

    text = notify.format_digest(new_rows, standing_rows, warnings,
                                first_run=first_run, now=stamp)
    if not dry_run:
        notify.send(text)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rank open unclaimed bounties.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the digest instead of sending it")
    parser.add_argument("--sources", default=None,
                        help="comma-separated source names "
                             f"(default: {','.join(config.HUNT_ENABLED_SOURCES)})")
    args = parser.parse_args(argv)

    names = (args.sources.split(",") if args.sources else config.HUNT_ENABLED_SOURCES)
    conn = store.connect()
    text = run(conn, sources=build_sources(names), dry_run=args.dry_run)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
