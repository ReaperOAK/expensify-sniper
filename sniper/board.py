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

Rendering is a pure function of rows so it can be tested without a database, a
network, or a filesystem.
"""

from __future__ import annotations

from . import notify
from .models import _now_utc, parse_iso

TITLE_CHARS = 90


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
