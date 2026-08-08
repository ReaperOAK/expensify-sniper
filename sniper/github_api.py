"""Repo-agnostic GitHub access layer.

Every function takes an explicit `repo` ("owner/name"). Nothing here knows about
Expensify; that lives in sniper/sources/expensify.py.

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
from typing import Any, Iterator

import requests

from . import config

API = "https://api.github.com"
UA = "expensify-sniper/1.0"


class RateLimited(Exception):
    pass


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
        reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
        raise RateLimited(f"rate limited until {reset}")
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
