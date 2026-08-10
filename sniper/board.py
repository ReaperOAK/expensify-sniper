"""The bounty board: the whole inventory, rendered as one markdown page.

The digest is a shopping list -- short, ranked, pushed on a schedule, and
deliberately silent when there is nothing new, because 144 "nothing found"
messages a day would train the reader to ignore the channel.

The board is the opposite: everything, always, at a stable URL. It exists
because two things were invisible from the digest alone.

  * 529 live candidates carried no readable amount. They are correctly excluded
    from a ranked digest -- expected value cannot be computed without a price,
    and once the rankable ones ran out they padded it with 700-day-old issues
    titled "test" and "tttt". But "not rankable" became "unreachable", and the
    digest's own footer said so every three hours with no way to act on it.

  * The reject reasons were never displayed anywhere. 'open linked PR' had
    quietly grown to 562 -- larger than everything the tracker was keeping --
    and only a hand-written SQL query against the production database found it.

Rendering (`render`, `signature`, `has_changed`) is pure -- rows and dicts in,
a string or a bool out -- so it is tested without a database, a network, or a
filesystem. `create_gist` and `publish`, added later, are not: they are this
module's one outward-facing edge, and every call site that isn't a test-owned
fake goes to the real GitHub Gists API.
"""

from __future__ import annotations

import requests

from . import config, github_api, notify
from .models import _now_utc, parse_iso

TITLE_CHARS = 90
GIST_FILENAME = "bounties.md"
GIST_API = "https://api.github.com/gists"


def _get(row, key):
    """Read a column from either a sqlite3.Row or a plain dict.

    sqlite3.Row raises IndexError for an unknown column and dict raises
    KeyError, so one helper covers both and the renderer stays testable without
    a database.
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _fmt_row(row, *, now: float | None) -> str:
    """One board line. Mirrors notify.format_row, plus the competition marker."""
    line = notify.format_row(row, now=now)
    count = _get(row, "open_pr_count")
    if count:
        line += f" · ⚠ {count} PRs"
    return line


def _unrankable_line(row, *, now: float | None) -> str:
    title = row["title"]
    if len(title) > TITLE_CHARS:
        title = title[: TITLE_CHARS - 1] + "…"
    age = (_now_utc(now) - parse_iso(row["created_at"])).days
    return f"- [#{row['number']}]({row['url']}) {title} — {row['comments']}💬 · {age}d"


def render(rankable, unrankable, rejection_counts, *, now: float | None,
           warnings, totals: dict[str, int]) -> str:
    """The whole board. Warnings first, then priced, then unpriced, then rejects."""
    parts: list[str] = ["# Bounty board", ""]

    for warning in warnings:
        parts.append(f"> ⚠ {warning}")
    if warnings:
        parts.append("")

    if not rankable and not unrankable and not rejection_counts:
        parts.append("_The last sweep found nothing at all — no live candidates, "
                     "and nothing rejected. That is a sweep that ran and came back "
                     "empty, not a sweep that failed; check watch.log for liveness._")
        parts.append("")

    if rankable:
        parts.append(f"## Rankable — {len(rankable)} with a readable amount")
        parts.append("")
        parts.extend(_fmt_row(row, now=now) for row in rankable)
        parts.append("")

    if unrankable:
        parts.append(f"## No readable amount — {len(unrankable)} live, unranked")
        parts.append("")
        parts.append("_No price in the labels, title or body, so expected value "
                     "cannot be computed and the digest will never show them. "
                     "Grouped by repo, most recently discovered first._")
        parts.append("")
        by_repo: dict[str, list] = {}
        for row in unrankable:
            by_repo.setdefault(row["repo"], []).append(row)
        for repo, rows in by_repo.items():
            parts.append(f"### {repo}")
            parts.extend(_unrankable_line(row, now=now) for row in rows)
            parts.append("")

    if rejection_counts:
        parts.append("## Rejected")
        parts.append("")
        parts.append("_Why candidates were dropped. A reason that grows larger "
                     "than the kept pile is a filter to go and read._")
        parts.append("")
        parts.append("| count | reason |")
        parts.append("| ----: | ------ |")
        for reason, count in rejection_counts:
            parts.append(f"| {count} | {reason} |")
        parts.append("")

    swept = _now_utc(now).strftime("%Y-%m-%d %H:%M UTC")
    summary = " · ".join(f"{key} {value}" for key, value in totals.items())
    parts.append("---")
    parts.append(f"_Swept {swept}{' · ' + summary if summary else ''}_")
    return "\n".join(parts)


def signature(unrankable_rows) -> dict:
    """A cheap fingerprint of the unrankable set: how many, and from where."""
    repos = sorted({row["repo"] for row in unrankable_rows})
    return {"count": len(unrankable_rows), "repos": repos}


def has_changed(previous: dict | None, current: dict, *, pct: float) -> bool:
    """Is this board worth putting a link in front of the operator for?

    The digest already refuses to say "nothing new" every ten minutes, for a
    reason that applies here too: a channel that speaks constantly stops being
    read, and the one message that matters then arrives somewhere nobody looks.

    So a link is worth sending when a repo appears that was not there before --
    genuinely new inventory -- or when the count moves by more than `pct`.
    Everything else is drift.

    The asymmetry is deliberate, not an oversight: a NEW repo always links,
    unconditionally, but a repo dropping out of the set does not -- it only
    links if the resulting count move clears `pct`. A new repo is new
    inventory, worth a look regardless of size. A repo disappearing is
    something going away, and only the count check downstream of it can say
    whether that was one issue or four hundred.
    """
    if previous is None:
        return True
    if set(current["repos"]) - set(previous.get("repos") or []):
        return True
    before = previous.get("count") or 0
    if before == 0:
        return current["count"] > 0
    return abs(current["count"] - before) / before * 100.0 > pct


def create_gist(text: str, *, post=None) -> tuple[str, str]:
    """Create the private gist this board lives in. Returns (id, url).

    Run once by hand via `hunt.py --init-gist`; the id then goes in .env as
    BOUNTY_GIST_ID so every later sweep updates the SAME url. A gist created per
    sweep would hand the operator a bookmark that dies every ten minutes.

    Headers come from github_api._headers() rather than a second copy: that
    function already carries the User-Agent GitHub's API expects, and a header
    change (an API-version bump, an auth scheme change) applied to one copy and
    not the other is exactly the kind of drift a shared helper exists to
    prevent.
    """
    poster = requests.post if post is None else post
    resp = poster(GIST_API, json={
        "description": "expensify-sniper bounty board",
        "public": False,
        "files": {GIST_FILENAME: {"content": text}},
    }, headers=github_api._headers(), timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    return payload["id"], payload["html_url"]


def publish(text: str, *, post=None) -> str | None:
    """Update the configured gist. Returns its url, or None if none is set.

    None is a real answer and the caller must say it out loud. An unset gist id
    silently succeeding is the same failure mode as an unset Discord webhook:
    every run reports success while nothing reaches anybody.
    """
    if not config.BOUNTY_GIST_ID:
        return None
    patcher = requests.patch if post is None else post
    resp = patcher(f"{GIST_API}/{config.BOUNTY_GIST_ID}",
                   json={"files": {GIST_FILENAME: {"content": text}}},
                   headers=github_api._headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()["html_url"]
