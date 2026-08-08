"""Digest formatting and delivery.

Discord webhook by default, Telegram by env var. Both are "POST JSON to a URL",
so this is one branch rather than an abstraction layer -- config.py already
declares DISCORD_WEBHOOK, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.

Warnings render above the results. A run that could not reach a source has to
say so somewhere unmissable: a silently short list reads as "there is nothing
out there", which is exactly the failure HANDOFF.md trap #3 describes.
"""

from __future__ import annotations

import requests

from . import config
from .models import _now_utc, parse_iso

# Discord caps an embed description at 4096 characters; 3800 leaves room for the
# title and a safety margin on multi-byte titles.
DISCORD_LIMIT = 3800
TELEGRAM_LIMIT = 4000
TITLE_CHARS = 70


def format_row(row, *, now: float | None = None) -> str:
    """One digest line: amount, repo#number, title, comments, age, language, link."""
    amount = f"${row['amount_usd']}" if row["amount_usd"] is not None else "$?"
    title = row["title"]
    if len(title) > TITLE_CHARS:
        title = title[: TITLE_CHARS - 1] + "…"
    language = row["language"] or "—"
    age = (_now_utc(now) - parse_iso(row["created_at"])).days
    return (f"`{amount:>7}` **{row['repo']}#{row['number']}** {title}\n"
            f"    {row['comments']}💬 · {age}d · {language} · {row['url']}")


def format_digest(new_rows, standing_rows, warnings, *, first_run: bool,
                  now: float | None = None) -> str:
    """Render the whole digest. Warnings first, always."""
    parts: list[str] = []

    for warning in warnings:
        parts.append(f"⚠ {warning}")
    if warnings:
        parts.append("")

    if first_run:
        parts.append(f"**Bounty backlog — top {len(new_rows)} by score**")
        parts.extend(format_row(row, now=now) for row in new_rows)
        return "\n".join(parts)

    if new_rows:
        parts.append(f"**{len(new_rows)} new since last run**")
        parts.extend(format_row(row, now=now) for row in new_rows)
    else:
        parts.append("No new bounties since last run.")

    if standing_rows:
        parts.append("")
        parts.append(f"**Top {len(standing_rows)} standing**")
        parts.extend(format_row(row, now=now) for row in standing_rows)

    return "\n".join(parts)


def chunk(text: str, limit: int) -> list[str]:
    """Split on line boundaries, hard-splitting any single line that is too long."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send(text: str, *, post=None) -> str:
    """Deliver the digest. Returns the channel used: discord, telegram or none."""
    poster = requests.post if post is None else post

    if config.DISCORD_WEBHOOK:
        for part in chunk(text, DISCORD_LIMIT):
            resp = poster(config.DISCORD_WEBHOOK,
                          json={"embeds": [{"title": "Bounty digest",
                                            "description": part}]},
                          timeout=30)
            resp.raise_for_status()
        return "discord"

    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        for part in chunk(text, TELEGRAM_LIMIT):
            resp = poster(url,
                          json={"chat_id": config.TELEGRAM_CHAT_ID, "text": part,
                                "disable_web_page_preview": True},
                          timeout=30)
            resp.raise_for_status()
        return "telegram"

    return "none"
