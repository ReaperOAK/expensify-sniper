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

from sniper import config, filters, github_api, notify, payers
from sniper import score as scoring, store  # noqa: E402
from sniper.models import Candidate  # noqa: E402
from sniper.sources.base import Source, SourceResult  # noqa: E402
from sniper.sources.bounty_labels import BountyLabelSource  # noqa: E402
from sniper.sources.expensify import ExpensifySource
from sniper.sources.payer_watch import PayerWatchSource  # noqa: E402

SOURCE_CLASSES = {
    BountyLabelSource.name: BountyLabelSource,
    ExpensifySource.name: ExpensifySource,
    PayerWatchSource.name: PayerWatchSource,
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
        if details and config.CORE_CALL_PAUSE_S:
            # Burst guard, not quota: run 1 enriches every unique repo in the
            # backlog back to back, which is hundreds of requests in seconds.
            time.sleep(config.CORE_CALL_PAUSE_S)
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
    notify_when_empty: bool = True,
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

    # Stage 0 of the rejects -- the operator's own verdicts. These outrank every
    # automated signal: a human followed a link and saw something metadata cannot.
    dismissed = store.dismissed_reasons(conn)
    # Retroactive, not just prospective: an operator dismisses an issue because
    # they just read it in a digest, so it is already stored and already live.
    store.apply_dismissals(conn)

    # The open-linked-PR rule was replaced by a claim test. Rows it rejected under
    # the old question have to be put back in front of the new one, or they stay
    # rejected until GitHub happens to touch them again -- which for a months-old
    # bounty is never. Costs one UPDATE and is a no-op once the table is drained.
    revived = store.clear_rejections(conn, "open linked PR")
    if revived:
        warnings.append(f"{revived} candidates re-judged: an open PR is no longer "
                        f"treated as a claim unless its author is assigned")

    # Payout history first: one cached search for the whole run, and stage 1 needs
    # it. Whether three $-labels mean a spam farm or a tiered bounty is not
    # decidable from the labels -- only from whether the repo has ever paid.
    payer_repos, payer_warnings = payers.known_payers(conn, now=stamp)
    warnings.extend(payer_warnings)

    def closed_for(repo: str, label: str) -> int | None:
        return payers.ever_closed_with(conn, repo, label, now=stamp)

    store.apply_payout_rejections(conn, payer_repos, config.UNPAID_REPO_THRESHOLD,
                                  closed_for)

    # Stage 1 -- free rejects. Search payload only; zero API calls.
    survivors: list[Candidate] = []
    for cand in discovered:
        if (cand.repo, cand.number) in dismissed:
            note = dismissed[(cand.repo, cand.number)]
            store.upsert_candidate(
                conn, cand, now=stamp,
                rejected=f"dismissed by operator{': ' + note if note else ''}")
            continue
        trusted = cand.repo in payer_repos
        reason = filters.free_reject(cand, trusted=trusted)
        if reason:
            store.upsert_candidate(conn, cand, rejected=reason, now=stamp)
        else:
            survivors.append(cand.with_conservative_amount() if trusted else cand)


    open_here: dict[str, int] = {}
    for cand in survivors:
        open_here[cand.repo] = open_here.get(cand.repo, 0) + 1
    kept: list[Candidate] = []
    for cand in survivors:
        # The guard matters: without it every candidate costs a search unit,
        # and payout_reject would abstain on nearly all of them anyway (below
        # threshold, or already on the payer roster). ever_closed_with caches
        # per (repo, label), so repeated candidates in one repo cost one call.
        label = payers.bounty_label_of(cand.labels)
        closed = (closed_for(cand.repo, label)
                  if label and cand.repo not in payer_repos
                  and open_here[cand.repo] >= config.UNPAID_REPO_THRESHOLD
                  else None)
        reason = payers.payout_reject(
            cand.repo, cand.labels, open_here[cand.repo], payer_repos, closed)
        if reason:
            store.upsert_candidate(conn, cand, rejected=reason, now=stamp)
        else:
            kept.append(cand)
    survivors = kept

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
        if config.CORE_CALL_PAUSE_S:
            time.sleep(config.CORE_CALL_PAUSE_S)
        try:
            linked = github_api.linked_pr_state(
                cand.repo, cand.number, frozenset(cand.assignees))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"timeline for {cand.repo}#{cand.number} failed: {exc}")
            continue
        # An open PR by someone already assigned is the one shape that means the
        # work has been handed out -- true on Expensify, where a C+ picks a
        # proposal and then assigns its author, and on Algora, where the winner
        # is whoever merges. Every other open PR is a rival, and rivals are
        # scored rather than rejected.
        if linked.claimant:
            store.upsert_candidate(
                conn, cand, rejected=f"claimed by assignee @{linked.claimant}",
                now=stamp)
            continue
        cand = cand.with_linked_prs(linked.open_count)
        try:
            scored = cand.with_score(scoring.score(cand, now=stamp))
        except Exception as exc:  # noqa: BLE001 -- score() raises by contract on an
            # unenriched candidate. Unreachable today (with_repo_detail sets every
            # field from one payload, and repo_reject already passed), but a 200
            # response missing one of those keys would otherwise kill the whole
            # run: no digest, and every other warning in it lost too.
            warnings.append(f"scoring {cand.repo}#{cand.number} failed: {exc}")
            continue
        store.upsert_candidate(conn, scored, now=stamp)

    # Stage 4 -- digest.
    if first_run:
        new_rows = store.live_candidates(conn, config.DIGEST_TOP_N,
                                         max_per_repo=config.DIGEST_MAX_PER_REPO,
                                         require_amount=True)
        standing_rows = []
    else:
        new_rows = store.live_candidates(conn, config.DIGEST_TOP_N, since=stamp,
                                         max_per_repo=config.DIGEST_MAX_PER_REPO,
                                         require_amount=True)
        standing_rows = store.live_candidates(conn, config.DIGEST_STANDING_N,
                                              max_per_repo=config.DIGEST_MAX_PER_REPO,
                                              require_amount=True)

    held_back = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE rejected IS NULL AND amount_usd IS NULL"
    ).fetchone()[0]
    text = notify.format_digest(new_rows, standing_rows, warnings,
                                first_run=first_run, now=stamp,
                                held_back=held_back)
    # A sweep that found nothing and hit no trouble is not news. The watch runs
    # every ten minutes: delivering "nothing new" each time is 144 messages a
    # day, which does not merely annoy -- it trains the reader to ignore the
    # channel, so the one message that matters arrives somewhere nobody looks.
    #
    # Warnings ALWAYS deliver. "Nothing found" and "I could not look" must never
    # be the same silence, which is the failure this whole pipeline is built
    # against. Silence here means, and only means: I swept, and there was
    # nothing. watch.log records every sweep either way, so liveness is provable
    # without spending the reader's attention.
    nothing_to_say = not new_rows and not warnings and not first_run
    if nothing_to_say and not notify_when_empty:
        return text

    if not dry_run:
        # Delivery is the one stage whose failure a human would never see, which
        # makes it the one that most needs saying out loud. An unset webhook
        # returns "none" and does nothing at all -- every 3-hourly run would
        # "succeed" forever while notifying no one. And a revoked webhook raises,
        # which would take the assembled digest down with it AFTER the candidates
        # in it were already committed as seen, so they never reappear as new.
        try:
            channel = notify.send(text)
        except Exception as exc:  # noqa: BLE001
            channel = "failed"
            text += f"\n\n⚠ digest delivery failed: {exc}"
        if channel in ("none", "failed"):
            reason = ("no delivery channel configured (set DISCORD_WEBHOOK, or "
                      "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)"
                      if channel == "none" else "delivery raised")
            print(f"⚠ digest was NOT delivered: {reason}", file=sys.stderr)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rank open unclaimed bounties.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the digest instead of sending it")
    parser.add_argument("--watch", action="store_true",
                        help="fast sweep of proven-payer repos only "
                             "(cheap: core quota, and a 304 costs nothing)")
    parser.add_argument("--dismiss", metavar="REPO#NUMBER", default=None,
                        help="never show this issue again (e.g. commaai/opendbc#3426)")
    parser.add_argument("--undismiss", metavar="REPO#NUMBER", default=None,
                        help="reverse a --dismiss")
    parser.add_argument("--reason", default=None,
                        help="why, recorded alongside --dismiss")
    parser.add_argument("--list-dismissed", action="store_true",
                        help="print every dismissal and exit")
    parser.add_argument("--sources", default=None,
                        help="comma-separated source names "
                             f"(default: {','.join(config.HUNT_ENABLED_SOURCES)})")
    args = parser.parse_args(argv)

    conn = store.connect()

    if args.list_dismissed:
        rows = store.dismissed_reasons(conn)
        if not rows:
            print("nothing dismissed")
        for (repo, number), reason in sorted(rows.items()):
            print(f"{repo}#{number}  {reason or '(no reason recorded)'}")
        return 0
    if args.dismiss or args.undismiss:
        target = args.dismiss or args.undismiss
        if "#" not in target:
            print(f"expected REPO#NUMBER, got {target!r}", file=sys.stderr)
            return 2
        repo, _, num = target.rpartition("#")
        if args.dismiss:
            store.dismiss(conn, repo, int(num), args.reason)
            print(f"dismissed {repo}#{num}"
                  f"{' -- ' + args.reason if args.reason else ''}")
        else:
            found = store.undismiss(conn, repo, int(num))
            print(f"{'restored' if found else 'was not dismissed'}: {repo}#{num}")
        return 0

    if args.watch:
        names = config.WATCH_SOURCES
    else:
        names = (args.sources.split(",") if args.sources
                 else config.HUNT_ENABLED_SOURCES)
    text = run(conn, sources=build_sources(names), dry_run=args.dry_run)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
