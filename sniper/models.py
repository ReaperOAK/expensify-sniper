"""The Candidate -- one bounty, from any source, in one shape.

Amounts live in labels rather than a field: GitHub has no bounty concept, so
Algora and everyone else encode the number as a label ($780, $1k, $1.2k). That
makes label parsing load-bearing, and it makes a MULTI-amount issue meaningful:
SecureBananaLabs/bug-bounty#11398 carries $780, $1k and $1.2k at once, which is
a spam farm rather than a $3k bounty. amounts_in() therefore reports the whole
distinct set and leaves the judgement to filters.py.

Repo-detail fields (fork/archived/stars/pushed_at/language) start as None
because a search result item does not carry them -- measured 2026-08-05, search
items have only `repository_url`. None here means "not enriched yet", which is
deliberately distinguishable from a real value.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

# "$780", "$1k", "$1.2k", "$1,200", "$ 50". Anchored: a label merely CONTAINING a
# dollar sign (e.g. "sponsored $$$") is not an amount.
_AMOUNT_LABEL = re.compile(r"^\$\s*(\d+(?:[.,]\d+)*)\s*([kK])?$")


def parse_amount_label(label: str) -> int | None:
    """Return the USD amount a label encodes, or None if it is not an amount label."""
    match = _AMOUNT_LABEL.match((label or "").strip())
    if not match:
        return None
    digits, suffix = match.group(1), match.group(2)
    # "1,200" is thousands separators; "1.2" with a k suffix is a decimal.
    if suffix:
        value = float(digits.replace(",", ""))
        return int(value * 1000)
    return int(float(digits.replace(",", "")))


def amounts_in(labels: Iterable[str]) -> tuple[int, ...]:
    """Distinct amounts encoded by these labels, ascending."""
    found = {amount for label in labels
             if (amount := parse_amount_label(label)) is not None}
    return tuple(sorted(found))


def repo_from_url(repository_url: str) -> str:
    """"https://api.github.com/repos/acme/widgets" -> "acme/widgets"."""
    owner, name = repository_url.rstrip("/").rsplit("/", 2)[-2:]
    return f"{owner}/{name}"


def parse_iso(value: str) -> dt.datetime:
    """GitHub timestamps are "2026-08-05T06:20:46Z"; fromisoformat wants an offset."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now_utc(now: float | None) -> dt.datetime:
    stamp = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    return dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc)


@dataclass(frozen=True)
class Candidate:
    source: str
    repo: str
    number: int
    title: str
    url: str
    state: str
    comments: int
    labels: tuple[str, ...]
    created_at: str
    updated_at: str
    has_assignee: bool
    amount_usd: int | None
    amounts_seen: tuple[int, ...]
    # Filled by with_repo_detail(); None means "not enriched", never "zero".
    language: str | None = None
    stars: int | None = None
    pushed_at: str | None = None
    fork: bool | None = None
    archived: bool | None = None
    score: float | None = None

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.source, self.repo, self.number)

    @classmethod
    def from_issue(
        cls,
        source: str,
        repo: str,
        payload: dict[str, Any],
        *,
        amount_usd: int | None = None,
    ) -> Candidate:
        labels = tuple(label["name"] for label in payload.get("labels") or [])
        seen = () if amount_usd is not None else amounts_in(labels)
        amount = amount_usd
        if amount is None and len(seen) == 1:
            amount = seen[0]
        has_assignee = bool(payload.get("assignee") or payload.get("assignees"))
        return cls(
            source=source,
            repo=repo,
            number=payload["number"],
            title=payload.get("title", ""),
            url=payload.get("html_url", ""),
            state=payload.get("state", ""),
            comments=payload.get("comments", 0),
            labels=labels,
            created_at=payload.get("created_at", ""),
            updated_at=payload.get("updated_at", ""),
            has_assignee=has_assignee,
            amount_usd=amount,
            amounts_seen=seen,
        )

    @classmethod
    def from_search_item(cls, source: str, item: dict[str, Any]) -> Candidate:
        return cls.from_issue(source, repo_from_url(item["repository_url"]), item)

    def with_repo_detail(self, payload: dict[str, Any]) -> Candidate:
        return replace(
            self,
            fork=payload.get("fork"),
            archived=payload.get("archived"),
            stars=payload.get("stargazers_count"),
            pushed_at=payload.get("pushed_at"),
            language=payload.get("language"),
        )

    def with_score(self, value: float) -> Candidate:
        return replace(self, score=value)

    def age_days(self, now: float | None = None) -> int:
        """Days since the issue was created. Displayed, never scored."""
        return (_now_utc(now) - parse_iso(self.created_at)).days
