"""Repo-agnostic GitHub access layer.

Every function takes an explicit `repo` ("owner/name"), so no URL in this module
is tied to one project.

"Repo-agnostic" is a claim about URLs, not about filtering, and the difference
matters: poll_help_wanted_events() still filters on config.HELP_WANTED, and
existing_proposals() still uses config.is_proposal() and config.is_bot_author().
Those are Expensify's business rules rather than GitHub's, and they move out to a
dedicated source module later. Until they do, do not read this module as free of
Expensify.

The watcher polls /issues/events rather than /issues, because `Help Wanted` is
applied to issues that already exist. Sorting /issues by `created` (as the old
sniper.py did) means a freshly-labelled three-day-old issue never appears at the
top of the list and is missed entirely. The same trap applies to bounty labels,
which is why discovery in sources/bounty_labels.py sorts by `updated`.

Measured 2026-08-01: 100 events on Expensify/App spans ~2h44m, so a 20s poll with
per_page=100 has a very wide safety margin against bursts.

Two quota classes, and they are NOT interchangeable:
  core   -- 5000/hr. Supports ETags; a 304 costs zero quota.
  search -- 30/min. Measured 2026-08-05: search responses carry NO ETag, so every
            search call costs a unit and there is no revalidation to be had.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Iterator

import requests

from . import config

API = "https://api.github.com"
UA = "expensify-sniper/1.0"

# The Search API hard-caps at 1000 results regardless of total_count; asking for
# more returns a 422, so we stop ourselves first. The cap is in RESULTS, not
# pages, so it does not move when per_page does -- and it does move: production
# pages at config.SEARCH_PER_PAGE (50) over config.BOUNTY_SEARCH_MAX_PAGES (20),
# not at this function's own 100/10 defaults, because per_page=100 trips GitHub's
# burst guard on this query. Nothing calls those defaults.
SEARCH_RESULT_CAP = 1000


class RateLimited(Exception):
    """A 403 that means "slow down", with WHICH limit it was.

    GitHub has two, and they behave nothing alike:

    * primary -- the documented pools (search 30/min, core 5000/hr). Depletion is
      visible in X-RateLimit-Remaining and the reset epoch is meaningful.
    * secondary -- an undocumented burst/abuse guard. It fires regardless of
      primary quota (measured 2026-08-08: a 403 with X-RateLimit-Remaining 23 of
      30 still left) and its reply says only "wait a few minutes". The
      X-RateLimit-Reset header on that response describes the PRIMARY window and
      is therefore meaningless for deciding when to retry.

    Conflating them produces a confident, wrong "rate limited until <epoch>" that
    a caller cannot act on -- so `secondary` and `retry_after` are carried
    explicitly. `retry_after` is None when GitHub declined to say.
    """

    def __init__(self, message: str, *, retry_after: float | None = None,
                 secondary: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.secondary = secondary


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
    }
    if extra:
        headers.update(extra)
    return headers


def _get(path: str, params: dict[str, Any] | None = None, etag: str | None = None) -> requests.Response:
    url = path if path.startswith("http") else f"{API}{path}"
    extra = {"If-None-Match": etag} if etag else None
    resp = requests.get(url, headers=_headers(extra), params=params, timeout=30)
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        secondary = "secondary rate limit" in resp.text.lower()
        header = resp.headers.get("Retry-After")
        if header is not None:
            retry_after: float | None = float(header)
        elif secondary:
            # No Retry-After and no usable reset: GitHub says only "a few
            # minutes". Reporting the primary reset epoch here would be a
            # confident lie, so say nothing rather than something wrong.
            retry_after = None
        else:
            reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
            retry_after = max(0.0, reset - time.time())
        if secondary:
            raise RateLimited(
                "secondary rate limit (burst guard) -- primary quota was not "
                f"exhausted ({resp.headers.get('X-RateLimit-Remaining', '?')} of "
                f"{resp.headers.get('X-RateLimit-Limit', '?')} left); slow down",
                retry_after=retry_after, secondary=True)
        reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
        raise RateLimited(f"rate limited until {reset}", retry_after=retry_after)
    return resp


def poll_help_wanted_events(repo: str, etag: str | None) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Return (help-wanted label events newest-first, new etag, changed).

    A 304 costs no rate-limit quota, which is why the ETag is threaded through.
    """
    resp = _get(f"/repos/{repo}/issues/events", params={"per_page": 100}, etag=etag)
    if resp.status_code == 304:
        return [], etag, False
    resp.raise_for_status()

    events = []
    for event in resp.json():
        if event.get("event") != "labeled":
            continue
        if (event.get("label") or {}).get("name") != config.HELP_WANTED:
            continue
        issue = event.get("issue") or {}
        # Pull requests come through this endpoint too; they are never bounties.
        if issue.get("pull_request"):
            continue
        events.append(
            {
                "event_id": event["id"],
                "issue": issue.get("number"),
                "title": issue.get("title", ""),
                "state": issue.get("state"),
                "labeled_at": event.get("created_at"),
                "labels": [label["name"] for label in issue.get("labels", [])],
            }
        )
    return events, resp.headers.get("ETag"), True


def get_issue(repo: str, number: int) -> dict[str, Any]:
    resp = _get(f"/repos/{repo}/issues/{number}")
    resp.raise_for_status()
    return resp.json()


def iter_comments(repo: str, number: int) -> Iterator[dict[str, Any]]:
    page = 1
    while True:
        resp = _get(f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100, "page": page})
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            return
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def existing_proposals(repo: str, number: int) -> list[dict[str, Any]]:
    """Human proposals already on the issue, oldest first.

    Mirrors the filtering in proposalPoliceComment.ts:137-149 -- bot authors are
    skipped because ProposalPolice never compares against them, so they are not
    competition and cannot get us withdrawn.
    """
    proposals = []
    for comment in iter_comments(repo, number):
        body = comment.get("body") or ""
        login = (comment.get("user") or {}).get("login")
        if config.is_bot_author(login) or (comment.get("user") or {}).get("type") == "Bot":
            continue
        if not config.is_proposal(body):
            continue
        proposals.append(
            {
                "author": login,
                "created_at": comment.get("created_at"),
                "url": comment.get("html_url"),
                "body": body,
            }
        )
    proposals.sort(key=lambda item: item["created_at"] or "")
    return proposals


def melvin_analysis(repo: str, number: int) -> str | None:
    """MelvinBot's own root-cause comment, if present.

    Worth reading precisely because ProposalPolice ignores it: every human converges
    on it, so it is the single best predictor of what the duplicate cluster will say.
    """
    for comment in iter_comments(repo, number):
        login = (comment.get("user") or {}).get("login")
        if login in {"MelvinBot", "melvin-bot[bot]"} and "root cause" in (comment.get("body") or "").lower():
            return comment.get("body")
    return None


def post_comment(repo: str, number: int, body: str) -> dict[str, Any]:
    resp = requests.post(
        f"{API}/repos/{repo}/issues/{number}/comments",
        headers=_headers(),
        data=json.dumps({"body": body}),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def rate_limit() -> dict[str, Any]:
    resp = _get("/rate_limit")
    resp.raise_for_status()
    return resp.json()["resources"]["core"]


def wait_for_reset(reset_epoch: int) -> None:
    time.sleep(max(0, reset_epoch - time.time()) + 1)


def _fetch_search_page(query, sort, order, per_page, page, *,
                       retries, backoff, sleep, have_items=True):
    """One search page, retrying a SECONDARY throttle. None = give up gracefully.

    A secondary (burst) limit is a few minutes of cooldown and then it is gone,
    so abandoning a query over one is expensive: measured live, giving up cost
    420 of 520 candidates and produced a digest dominated by whichever repo
    happened to fill page one. A PRIMARY limit needs the whole rate window to
    roll, which is longer than a run should sit blocking, so it is not retried.

    Raising versus returning None is the caller's "have I got anything yet"
    question, so the caller passes `have_items` and this never has to guess.
    """
    for attempt in range(retries + 1):
        try:
            return _get(
                "/search/issues",
                params={"q": query, "sort": sort, "order": order,
                        "per_page": per_page, "page": page},
            )
        except RateLimited as exc:
            out_of_attempts = attempt == retries
            if exc.secondary and not out_of_attempts:
                sleep(exc.retry_after or backoff * (2 ** attempt))
                continue
            if have_items:
                return None
            raise
    return None


@dataclass(frozen=True)
class SearchResult:
    """Outcome of a paged search.

    `incomplete` mirrors GitHub's `incomplete_results`: the query timed out
    server-side and the items are a partial answer. It is a field rather than a
    log line because the digest has to SAY it could not see everything -- a
    silently short result set is exactly HANDOFF.md trap #3.
    """

    items: list[dict[str, Any]]
    total_count: int
    incomplete: bool
    pages_fetched: int


def search_issues(
    query: str,
    *,
    sort: str = "updated",
    order: str = "desc",
    per_page: int = 100,
    max_pages: int = 10,
    stop: Callable[[list[dict[str, Any]]], bool] | None = None,
    pause_s: float | None = None,
    retries: int | None = None,
    backoff_s: float | None = None,
    sleep=time.sleep,
) -> SearchResult:
    """Page /search/issues for `query`, newest-updated first.

    Costs one unit of the 30/min search quota per page: search responses carry no
    ETag (measured 2026-08-05), so unlike the core API there is nothing to
    revalidate against.

    `sort="updated"` is not a preference. Bounty labels are applied to issues that
    already exist -- measured 2026-08-05, only 1 of 521 live candidates was created
    in the previous 30 days -- so `created` ordering misses essentially everything.

    `stop` is called with each full, under-cap page's items; returning True ends
    paging. Discovery uses it to stop once a page is entirely already-known. It is
    not called on a page that already ended the loop by another route.
    """
    items: list[dict[str, Any]] = []
    total_count = 0
    incomplete = False
    pages_fetched = 0

    pause = config.SEARCH_PAGE_PAUSE_S if pause_s is None else pause_s
    retries = config.SEARCH_MAX_RETRIES if retries is None else retries
    backoff = config.SEARCH_RETRY_BACKOFF_S if backoff_s is None else backoff_s

    for page in range(1, max_pages + 1):
        # Pace the pages. The primary 30/min pool is not the binding constraint
        # here -- measured 2026-08-08, ten back-to-back pages tripped GitHub's
        # SECONDARY (burst) limit with 23 of 30 primary units still unspent, and
        # a 3-hourly cron would have hit it on every single run. The budget guard
        # in store.py cannot prevent this: it models the documented pools, and
        # the secondary limit is about rate, not volume.
        if page > 1 and pause:
            sleep(pause)
        resp = _fetch_search_page(
            query, sort, order, per_page, page,
            retries=retries, backoff=backoff, sleep=sleep,
            have_items=bool(items),
        )
        if resp is None:
            # Throttled past our patience. Keep the pages already paid for:
            # letting this propagate threw away every completed page -- measured
            # live, three good pages were binned and the digest said "top 0 by
            # score", reporting an empty world because of a throttle on page
            # four. With nothing in hand there is nothing to salvage, so
            # _fetch_search_page re-raises instead of returning None.
            incomplete = True
            break
        resp.raise_for_status()
        payload = resp.json()
        pages_fetched += 1

        total_count = payload.get("total_count", 0)
        incomplete = incomplete or bool(payload.get("incomplete_results"))
        batch = payload.get("items", [])
        items.extend(batch)

        if len(items) >= SEARCH_RESULT_CAP:
            del items[SEARCH_RESULT_CAP:]
            break
        # Without this, a total_count that is an exact multiple of per_page (100,
        # 200, ...) costs one extra request for a guaranteed-empty page -- a wasted
        # unit of the 30/min search pool, and not a rare shape for a label query.
        if len(items) >= total_count:
            break
        if len(batch) < per_page:
            break
        if stop is not None and stop(batch):
            break

    return SearchResult(items=items, total_count=total_count,
                        incomplete=incomplete, pages_fetched=pages_fetched)


def get_repo(repo: str, etag: str | None = None) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Return (repo detail, etag, changed) for "owner/name".

    Supplies fork / archived / stargazers_count / pushed_at / language -- none of
    which appear in a search result item (measured 2026-08-05), so this call is
    what turns a search hit into something scoreable.

    Core quota, and a 304 costs nothing: verified 2026-08-05 that revalidating with
    If-None-Match left X-RateLimit-Remaining unchanged. `changed=False` means "use
    your cached body", which is a different thing from "no such repo" -- the latter
    raises.
    """
    resp = _get(f"/repos/{repo}", etag=etag)
    if resp.status_code == 304:
        return None, etag, False
    resp.raise_for_status()
    return resp.json(), resp.headers.get("ETag"), True


def iter_timeline(repo: str, number: int) -> Iterator[dict[str, Any]]:
    """Yield timeline events for an issue, oldest first.

    Core quota (5000/hr), not search quota (30/min). That choice is the whole
    reason this uses /timeline rather than a `linked:pr` search.
    """
    page = 1
    while True:
        resp = _get(
            f"/repos/{repo}/issues/{number}/timeline",
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            return
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def has_open_linked_pr(repo: str, number: int) -> bool:
    """True if an OPEN pull request cross-references this issue.

    Someone is mid-flight; the bounty is effectively taken even though no one is
    assigned. Cross-references from plain issues (including issues in unrelated
    repos, which do occur) are ignored -- only a `pull_request` source counts.
    """
    for event in iter_timeline(repo, number):
        if event.get("event") != "cross-referenced":
            continue
        source = (event.get("source") or {}).get("issue") or {}
        if source.get("pull_request") and source.get("state") == "open":
            return True
    return False
