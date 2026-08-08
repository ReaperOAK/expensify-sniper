#!/usr/bin/env python3
"""Record live GitHub API responses as test fixtures. Run by hand, not by tests.

Usage:  python3 tools/record_fixtures.py

Writes tests/fixtures/*.json. Re-running overwrites them, which is the point:
when GitHub changes a payload shape, re-record and let the characterization
tests show exactly what moved.

Only public response bodies are written. No credential ever reaches a fixture.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from sniper import config  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
API = "https://api.github.com"

HEADERS = {
    "Authorization": f"Bearer {config.GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "expensify-sniper-fixture-recorder/1.0",
}

BOUNTY_QUERY = 'label:"💎 Bounty" state:open is:issue no:assignee -label:"💰 Rewarded"'

TARGETS = [
    # name, path, params
    ("search_bounty_page1", "/search/issues",
     {"q": BOUNTY_QUERY, "sort": "updated", "order": "desc", "per_page": 100}),
    ("search_spam_farm", "/search/issues",
     {"q": "repo:SecureBananaLabs/bug-bounty state:open is:issue", "per_page": 100,
      "sort": "updated", "order": "desc"}),
    ("search_fork_rewarded", "/search/issues",
     {"q": 'repo:WillSmithTE/qdrant-qdrant is:issue label:"💎 Bounty"', "per_page": 10}),
    ("repo_healthy", "/repos/ClankerNation/OpenAgents", {}),
    ("repo_fork", "/repos/WillSmithTE/qdrant-qdrant", {}),
    ("repo_spam_farm", "/repos/SecureBananaLabs/bug-bounty", {}),
    # Confirmed 2026-08-08 to carry 6 open pull-request cross-references. Cross-refs
    # churn fast -- if the Step 6 check reports False, re-run the finder in Step 6's
    # troubleshooting note and swap this target rather than weakening the assertion.
    ("timeline_with_open_pr", "/repos/arakoodev/EdgeChains/issues/279/timeline",
     {"per_page": 100}),
    ("events_expensify", f"/repos/{config.REPO}/issues/events", {"per_page": 100}),
]


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, path, params in TARGETS:
        resp = requests.get(f"{API}{path}", headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        out = FIXTURES / f"{name}.json"
        out.write_text(json.dumps(resp.json(), indent=1, ensure_ascii=False), encoding="utf-8")
        size = out.stat().st_size
        print(f"{name:24s} {resp.status_code}  {size:>9,d} bytes  etag={bool(resp.headers.get('ETag'))}")


if __name__ == "__main__":
    main()
