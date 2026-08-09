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
#
# The whole part is EITHER properly-grouped thousands (1,200 / 12,345) OR a plain
# run of digits, followed by AT MOST ONE decimal group. Being this strict is the
# point: a looser `(\d+(?:[.,]\d+)*)` matches "$1.2.3" and then float() raises
# mid-parse, and matches "$1,2,3" and silently yields 123. Every label on every
# issue goes through here unattended, so an unparseable label has to come back as
# None -- not as an exception, and not as a plausible wrong number that would
# propagate straight into the ranking.
_AMOUNT_LABEL = re.compile(r"^\$\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*([kK])?$")


def parse_amount_label(label: str) -> int | None:
    """Return the USD amount a label encodes, or None if it is not an amount label."""
    match = _AMOUNT_LABEL.match((label or "").strip())
    if not match:
        return None
    whole, fraction, suffix = match.group(1), match.group(2) or "", match.group(3)
    # The regex guarantees this is float()-able, so there is no failure path here.
    value = float(whole.replace(",", "") + fraction)
    return int(value * 1000) if suffix else int(value)


def amounts_in(labels: Iterable[str]) -> tuple[int, ...]:
    """Distinct amounts encoded by these labels, ascending."""
    found = {amount for label in labels
             if (amount := parse_amount_label(label)) is not None}
    return tuple(sorted(found))


# Amounts embedded in a title: "[ Bounty $9k ]", "[$10k bounty]", "[$10]".
# Deliberately NOT anchored -- unlike a label, a title is prose and the amount is
# somewhere inside it.
_AMOUNT_IN_TEXT = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*([kK])?\b")


def amounts_in_title(title: str) -> tuple[int, ...]:
    """Distinct amounts mentioned in an issue title, ascending.

    Plenty of real bounties never carry a $-label: commaai/opendbc#3426 is
    "[$10k bounty] Ford F-150 2026 (TRON) support" with the number only in the
    title. Scoring those at zero buried genuine, well-funded work underneath a
    bounty farm whose issues happen to be labelled properly.

    A title is weaker evidence than a label -- it is prose, and "$5 gift card"
    parses the same as a bounty -- so this is only ever a FALLBACK, used when the
    labels say nothing at all.
    """
    found = set()
    for whole, fraction, suffix in _AMOUNT_IN_TEXT.findall(title or ""):
        value = float(whole.replace(",", "") + (fraction or ""))
        found.add(int(value * 1000) if suffix else int(value))
    return tuple(sorted(found))


# "$500 bounty", "bounty: $500", "reward — $1.5k". Requires the WORD near the
# number: an issue body is long prose and plain "$5" in it is far more often a
# price, a gas fee or an example than a prize.
_BOUNTY_WORD = r"(?:bount(?:y|ies)|reward|prize|payout)"
_AMOUNT = r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*([kK])?\b"
# [^\n$]{0,25} rather than \W{0,25}: the gap is usually words -- "bounty of $500",
# "reward for this issue: $1k" -- but must not cross a newline or another '$',
# which would let an unrelated figure elsewhere in the body match.
_BOUNTY_NEAR_AMOUNT = re.compile(
    rf"{_BOUNTY_WORD}[^\n$]{{0,25}}{_AMOUNT}"
    rf"|{_AMOUNT}[^\n$]{{0,25}}{_BOUNTY_WORD}",
    re.IGNORECASE,
)

# How much of a body to read. Bounty statements live at the top; the tail is
# reproduction steps and logs full of numbers that are not prizes.
BODY_SCAN_CHARS = 600


def amounts_in_body(body: str) -> tuple[int, ...]:
    """Distinct amounts stated NEAR the word bounty/reward/prize, ascending.

    Measured 2026-08-09 on a full backlog: 543 of 748 live candidates had no
    parseable amount from labels or title, and only 5 repos of 26 carried one at
    all. The amounts are mostly in the body. `body` already arrives in the search
    payload, so reading it costs no extra API call.
    """
    found = set()
    for match in _BOUNTY_NEAR_AMOUNT.finditer((body or "")[:BODY_SCAN_CHARS]):
        whole = match.group(1) or match.group(4)
        fraction = match.group(2) or match.group(5) or ""
        suffix = match.group(3) or match.group(6)
        value = float(whole.replace(",", "") + fraction)
        found.add(int(value * 1000) if suffix else int(value))
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
        # amounts_seen always records what the LABELS said, even when the caller
        # overrides the amount. Suppressing it would hide a disagreement between
        # an override and the issue's own labels -- and it is what the spam rule
        # (more than one distinct amount) reads.
        seen = amounts_in(labels)
        amount = amount_usd
        if amount is None and len(seen) == 1:
            amount = seen[0]
        if amount is None and not seen:
            # Labels said nothing. Fall back to the title, then the body -- each
            # only on an unambiguous single figure, for the same reason two
            # $-labels mean spam rather than a bigger prize. Evidence weakens as
            # we go: a label is structured, a title is short prose, a body is long
            # prose, so the body match additionally demands the word "bounty"
            # (or reward/prize/payout) next to the number.
            for candidate_amounts in (amounts_in_title(payload.get("title", "")),
                                      amounts_in_body(payload.get("body", ""))):
                if len(candidate_amounts) == 1:
                    amount = candidate_amounts[0]
                    break
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

    def with_conservative_amount(self) -> Candidate:
        """Price a tiered bounty at its LOWEST tier.

        A repo offering $50/$250/$500 is quoting scopes, not one prize, and which
        tier the work lands in is unknowable from metadata. Ranking it at $500
        would flatter it above single-price bounties that are certain; the low
        tier is the only figure the operator is actually promised.
        """
        if self.amount_usd is not None or not self.amounts_seen:
            return self
        return replace(self, amount_usd=min(self.amounts_seen))

    def with_score(self, value: float) -> Candidate:
        return replace(self, score=value)

    def age_days(self, now: float | None = None) -> int:
        """Days since the issue was created. Displayed, never scored."""
        return (_now_utc(now) - parse_iso(self.created_at)).days
