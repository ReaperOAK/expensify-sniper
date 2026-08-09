"""What a source is.

A source discovers Candidates and reports, explicitly, everything it could not
see. Warnings are part of the return value rather than a log line because the
digest has to SAY a source came up short -- a quietly shorter list reads as
"there is nothing out there", which is the failure mode HANDOFF.md trap #3
describes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

from ..models import Candidate


@dataclass
class SourceResult:
    candidates: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class Source(Protocol):
    name: str

    def discover(self, conn: sqlite3.Connection) -> SourceResult:
        ...
