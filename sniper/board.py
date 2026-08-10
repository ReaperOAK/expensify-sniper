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

from . import config, github_api
from .models import _now_utc, parse_iso

# 90, against notify's 70. A gist page is as wide as the reader's browser and
# each row is a whole line of it, while a Discord embed field wraps inside a
# narrow column -- so the board can afford the extra 20 characters that keep a
# "[HOLD for payment] ..." prefix from eating the whole title.
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


def _escape_cell(text: str) -> str:
    """Neutralise the two characters that would corrupt a markdown table row.

    An issue title is untrusted text pulled straight from GitHub. A literal
    '|' shifts every cell after it one column to the right for that one row;
    a raw newline ends the row early and starts a new (broken) one. Both are
    legal in a GitHub issue title and neither showed up in the fixtures that
    built the old bullet-list renderer, because a bullet line never had a
    column to shift.
    """
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", "")


def _fmt_stars(stars: int) -> str:
    """1340 -> '1.3k★'; 87 -> '87★'. Compact, the way GitHub itself shows it."""
    if stars >= 1000:
        return f"{stars / 1000:.1f}k★"
    return f"{stars}★"


def _notes_cell(row) -> str:
    """Stars, comment count, open-PR competition -- the context a bare $ lacks.

    All three are optional and independently absent: a fresh repo has no
    stars, a fresh issue has no comments, and most issues have no competing
    PR at all. `_get` is needed for stars and open_pr_count specifically
    because both are genuinely nullable columns (and the dict fixtures in the
    test suite often omit them entirely rather than setting them to None);
    comments is NOT NULL in the schema and always present in the fixtures, so
    it's read directly.
    """
    bits = []
    stars = _get(row, "stars")
    if stars:
        bits.append(_fmt_stars(stars))
    if row["comments"]:
        bits.append(f"{row['comments']}💬")
    count = _get(row, "open_pr_count")
    if count:
        bits.append(f"⚠{count} PRs")
    return " · ".join(bits)


def _priced_row(row, *, now: float | None) -> str:
    """One row of the 'Worth a look' table: amount, issue + title, age, lang, notes."""
    amount = row["amount_usd"]
    amount_cell = f"**${amount:,}**" if amount is not None else "$?"
    title = _escape_cell(row["title"])
    if len(title) > TITLE_CHARS:
        title = title[: TITLE_CHARS - 1] + "…"
    issue_cell = f"[{row['repo']}#{row['number']}]({row['url']})<br>{title}"
    age = (_now_utc(now) - parse_iso(row["created_at"])).days
    lang = row["language"] or "—"
    return f"| {amount_cell} | {issue_cell} | {age}d | {lang} | {_notes_cell(row)} |"


def _unrankable_line(row, *, now: float | None) -> str:
    """One bullet inside a repo's <details> block, not a table row -- there is
    no column to protect here, so only the newline that would break the
    bullet list itself needs neutralising."""
    title = row["title"].replace("\n", " ").replace("\r", "")
    if len(title) > TITLE_CHARS:
        title = title[: TITLE_CHARS - 1] + "…"
    age = (_now_utc(now) - parse_iso(row["created_at"])).days
    return f"- [#{row['number']}]({row['url']}) {title} — {row['comments']}💬 · {age}d"


def _showing_caption(shown: int, total: int) -> str:
    """`_showing 25 of 48_` -- printed even when shown == total.

    The old heading stayed silent about the count when nothing was cut, on
    the theory that saying "showing" implied a cut had happened. That made
    the silence itself meaningful, which is fragile: silence proves nothing
    was cut only if every code path that COULD cut also remembers to speak
    when it doesn't. Saying the shown/total pair unconditionally does the same
    job by construction -- the reader never has to trust an absence, only read
    two numbers that are always both there.
    """
    return f"_showing {shown} of {total}_"


def render(rankable, unrankable, rejection_counts, *, now: float | None,
           warnings, totals: dict[str, int],
           rankable_total: int, unrankable_total: int,
           rejection_examples=None) -> str:
    """The whole board: title, one-line summary, warnings, priced, unpriced, rejects.

    Rewritten 2026-08-10 from a page of undifferentiated bullets -- ~930 live
    unpriced rows alone, thousands of lines total on real data -- into a page
    scannable in ten seconds: a table for the priced rows (gists render GFM
    tables), and every unpriced repo collapsed into a <details> block so
    "everything, always" no longer means "scroll forever". No information that
    was on the page before is gone; it is regrouped and, for the unpriced
    section, hidden behind one click per repo rather than printed flat.

    `rankable_total` and `unrankable_total` are the counts from the DATABASE,
    not from the rows handed over -- those are pages, and a page size printed as
    an inventory count is a lie the board is specifically built to stop telling.
    They are required, with no default, so a new call site has to answer the
    question rather than silently inherit len(rows).

    `totals` is decoration layered on top of numbers this function can already
    derive on its own (`live` from rankable_total + unrankable_total, `rejected`
    from summing rejection_counts, which is the full reason table, not a page).
    When a key is present it wins -- production computes both from the same
    one-transaction query, so trusting it is free -- but its absence changes no
    number on the page, which is what lets every dict-fixture test above pass
    `totals={}` without the summary line going wrong.

    `rejection_examples` is decoration and may be omitted: its absence changes
    no number on the page, only how quickly a wrong filter is recognisable.
    """
    rejected_total = sum(count for _, count in rejection_counts)
    live_total = totals.get("live", rankable_total + unrankable_total)
    rejected_shown = totals.get("rejected", rejected_total)

    parts: list[str] = ["# Bounty board", ""]

    swept = _now_utc(now).strftime("%Y-%m-%d %H:%M UTC")
    parts.append(f"**{live_total:,} live** · {rankable_total:,} priced · "
                 f"{unrankable_total:,} unpriced · {rejected_shown:,} rejected · "
                 f"swept {swept}")
    parts.append("")

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
        parts.append("## Worth a look")
        parts.append("")
        parts.append("| amount | issue | age | lang | notes |")
        parts.append("| -----: | ----- | --: | ---- | ----- |")
        parts.extend(_priced_row(row, now=now) for row in rankable)
        parts.append("")
        parts.append(_showing_caption(len(rankable), rankable_total))
        parts.append("")

    if unrankable:
        parts.append(f"## Unpriced — {unrankable_total} live, no amount readable "
                     "from labels, title or body")
        parts.append("")
        parts.append("_Not rankable, so the digest will never show them. Grouped "
                     "by repo, most recently found first._")
        parts.append("")
        by_repo: dict[str, list] = {}
        for row in unrankable:
            by_repo.setdefault(row["repo"], []).append(row)
        for repo, rows in by_repo.items():
            # <details> is the single biggest win in this rewrite: it turns a
            # repo with 185 unpriced issues from 185 lines of flat scroll into
            # one clickable line, with nothing hidden -- only collapsed.
            noun = "issue" if len(rows) == 1 else "issues"
            parts.append(f"<details><summary><b>{repo}</b> — {len(rows)} {noun}</summary>")
            parts.append("")
            parts.extend(_unrankable_line(row, now=now) for row in rows)
            parts.append("")
            parts.append("</details>")
            parts.append("")
        parts.append(_showing_caption(len(unrankable), unrankable_total))
        parts.append("")

    if rejection_counts:
        examples = rejection_examples or {}
        parts.append(f"## Rejected — {rejected_total}")
        parts.append("")
        parts.append("_Why candidates were dropped. A reason that grows larger "
                     "than the kept pile is a filter to go and read._")
        parts.append("")
        # The examples column is the half that catches a wrong filter. A count
        # only says a reason is BIG -- 'open linked PR' sat at 562 and looked
        # like a busy world until someone opened three of them and found
        # ordinary bounties carrying a drive-by PR from a bot. Three links per
        # reason turns "go and write a SQL query" into "go and click".
        parts.append("| count | reason | examples |")
        parts.append("| ----: | ------ | -------- |")
        for reason, count in rejection_counts:
            shown = " · ".join(f"[{row['repo']}#{row['number']}]({row['url']})"
                               for row in examples.get(reason, ()))
            parts.append(f"| {count} | {_escape_cell(reason)} | {shown} |")
        parts.append("")

    return "\n".join(parts)


def signature(unrankable_rows, total: int) -> dict:
    """A cheap fingerprint of the unrankable set: how many, and from where.

    `total` is the count from the database, not len(rows), because the rows are
    a page capped at config.BOARD_UNRANKABLE_N (600). The true unrankable set is
    934 post-revive, so a count taken from the page pins at 600 on the first
    sweep and never moves again -- has_changed's percentage arm would be dead
    from day one, leaving only the new-repo arm working.

    `repos` IS taken from the page, and that is sound for the arm that uses it:
    the page is ordered by first_seen_at DESC, so a repo new to the set is at
    the front of it and cannot be truncated away. A repo falling off the tail is
    invisible here -- and that is exactly the case has_changed deliberately does
    not link on, leaving it to the count.
    """
    repos = sorted({row["repo"] for row in unrankable_rows})
    return {"count": total, "repos": repos}


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


def _gist_headers() -> dict[str, str]:
    """github_api's headers, but carrying the gist credential.

    Reading public bounties needs no scope at all, and GITHUB_TOKEN is
    deliberately a scopeless classic PAT so that losing it costs nothing.
    Writing a gist needs the `gist` scope, so it gets its own token rather than
    widening the read one -- BOUNTY_GIST_TOKEN, falling back to GITHUB_TOKEN.

    Measured 2026-08-10: with the scopeless token, POST /gists answers 404, not
    403. GitHub reports a missing scope as not-found, so an under-scoped
    credential looks exactly like a deleted gist. If publishing ever starts
    failing with 404, check the scope before believing the gist is gone.
    """
    return github_api._headers({"Authorization": f"Bearer {config.BOUNTY_GIST_TOKEN}"})


def create_gist(text: str, *, post=None) -> tuple[str, str]:
    """Create the private gist this board lives in. Returns (id, url).

    Run once by hand via `hunt.py --init-gist`; the id then goes in .env as
    BOUNTY_GIST_ID so every later sweep updates the SAME url. A gist created per
    sweep would hand the operator a bookmark that dies every ten minutes.

    Headers come from github_api._headers() rather than a second copy: that
    function already carries the User-Agent GitHub's API expects, and a header
    change (an API-version bump, an auth scheme change) applied to one copy and
    not the other is exactly the kind of drift a shared helper exists to
    prevent. Only the credential differs -- see _gist_headers().
    """
    poster = requests.post if post is None else post
    resp = poster(GIST_API, json={
        "description": "expensify-sniper bounty board",
        "public": False,
        "files": {GIST_FILENAME: {"content": text}},
    }, headers=_gist_headers(), timeout=30)
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
                   headers=_gist_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()["html_url"]
