"""Test support: fixture loading and a stand-in for requests.get.

No test in this suite is allowed to touch the network. Fixtures are recorded
once by tools/record_fixtures.py and committed; FakeGet replays them.

The queue is FIFO and strict -- a test that makes more calls than it queued
fails loudly rather than silently reusing the last response, because a helper
that quietly returns stale data for an unmade call is exactly the ambiguity
HANDOFF.md trap #3 warns about.
"""

import json
import os
from pathlib import Path

import pytest

# Keep the REAL credential out of the test process entirely.
#
# sniper.config loads .env with os.environ.setdefault(), so a value already in
# the environment wins and the real token is never read. This must run before
# any test module imports sniper.config -- pytest imports conftest first, which
# is the only reason this works here and would not work inside a fixture.
#
# This is not belt-and-braces. A failing test's traceback can render the frames
# of _headers(), and a live token printed into a terminal ends up in scrollback,
# CI logs, and session transcripts. No test needs a real token: every request is
# served by FakeGet, which never inspects the Authorization header.
os.environ["GITHUB_TOKEN"] = "test-token-not-a-real-credential"

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    """Return the parsed JSON body recorded under tests/fixtures/<name>.json."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status_code: int, body, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = "" if body is None else json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakeGet:
    def __init__(self):
        self._queue: list[FakeResponse] = []
        self.calls: list[tuple[str, dict]] = []

    def queue(self, status: int, body, headers: dict[str, str] | None = None) -> None:
        self._queue.append(FakeResponse(status, body, headers))

    def __call__(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if not self._queue:
            raise AssertionError(f"unqueued request to {url} params={params}")
        return self._queue.pop(0)


@pytest.fixture
def fake_get(monkeypatch):
    """Replace requests.get inside sniper.github_api with a scripted stub."""
    from sniper import github_api

    stub = FakeGet()
    monkeypatch.setattr(github_api.requests, "get", stub)
    yield stub


@pytest.fixture(autouse=True)
def no_budget_waiting(monkeypatch):
    """Never let a test sleep waiting for a rate-limit window to roll over.

    Plan 2's budget guard blocks for up to config.BUDGET_WAIT_S seconds when a
    quota window is exhausted. That is correct in a 3-hourly cron run and
    intolerable in a test suite, so it is zeroed everywhere. Tests that exercise
    the waiting behavior pass max_wait_s explicitly and inject their own sleep.

    raising=False: this attribute does not exist yet in Plan 1.
    """
    from sniper import config

    monkeypatch.setattr(config, "BUDGET_WAIT_S", 0, raising=False)
