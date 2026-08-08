"""Central config. Everything sensitive comes from .env; nothing is hardcoded."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Minimal .env loader so the package has no dependency on python-dotenv."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # Values containing spaces (e.g. the Gmail app password) must be quoted so
        # the file can also be `source`d from a shell; strip the quotes back off here.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_env()


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required setting {name} in {ROOT / '.env'}")
    return value


# --- credentials -------------------------------------------------------------
GITHUB_TOKEN = _require("GITHUB_TOKEN")
GITHUB_LOGIN = os.environ.get("GITHUB_LOGIN", "").strip()

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
GMAIL_IMAP_HOST = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com").strip()

EXPENSIFY_TEST_EMAIL = os.environ.get("EXPENSIFY_TEST_EMAIL", "").strip()
EXPENSIFY_BASE_URL = os.environ.get("EXPENSIFY_BASE_URL", "https://staging.new.expensify.com").strip()

# Long-lived headless auth (`claude setup-token`). Required when a child `claude`
# process runs under an isolated CLAUDE_CONFIG_DIR, which has no stored credentials.
CLAUDE_CODE_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# --- repo / runtime ----------------------------------------------------------
REPO = "Expensify/App"
APP_REPO_PATH = Path(os.environ.get("APP_REPO_PATH", Path.home() / "Documents" / "App"))
DB_PATH = Path(os.environ.get("SNIPER_DB", ROOT / "sniper.sqlite"))
RUNS_DIR = Path(os.environ.get("SNIPER_RUNS", ROOT / "runs"))
EVIDENCE_DIR = Path(os.environ.get("SNIPER_EVIDENCE", ROOT / "evidence"))

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))

# --- behaviour ---------------------------------------------------------------
# Post to GitHub unattended. Set to 0 to require Telegram approval instead.
AUTO_POST = os.environ.get("AUTO_POST", "1") == "1"

# ProposalPolice withdraws at similarity >= 90 (see App repo,
# .github/actions/javascript/proposalPoliceComment/proposalPoliceComment.ts:161).
# We abort well below that line: a withdrawn proposal costs the slot AND the attempt.
SIMILARITY_ABORT = int(os.environ.get("SIMILARITY_ABORT", "80"))

# Max earlier human proposals before we stop bidding at all. Each one is an
# independent >=90 roll against us, so late entries are increasingly lossy.
MAX_EXISTING_PROPOSALS = int(os.environ.get("MAX_EXISTING_PROPOSALS", "6"))

# --- ProposalPolice detection contract (mirrors App repo .github/libs/CONST.ts:55-57)
# All three must be present or the bot does not classify the comment as a
# proposal at all. PROPOSAL_KEYWORD is case-sensitive; headers match a lowercased body.
PROPOSAL_KEYWORD = "Proposal"
PROPOSAL_HEADER_A = "what is the root cause of that problem?"
PROPOSAL_HEADER_B = "what changes do you think we should make in order to solve the problem?"

# Authors ProposalPolice skips when hunting duplicates (CONST.ts:28-31). We skip
# them too: we can never be flagged against these, so they are not competition.
BOT_AUTHORS = {
    "melvin-bot[bot]",
    "MelvinBot",
    "chatgpt-codex-connector",
    "github-actions",
    "github-actions[bot]",
    "OSBotify",
    "imgbot[bot]",
    "codecov[bot]",
    "exfy-zapier",
}

HELP_WANTED = "Help Wanted"

# --- bounty tracker ----------------------------------------------------------
# Discovery queries. `sort`/`order` are NOT baked in here -- search_issues()
# always sorts by `updated`, because bounty labels land on issues that already
# exist (measured 2026-08-05: 1 of 521 live candidates was created in the
# previous 30 days, so `created` ordering misses essentially everything).
#
# `💎 Bounty` (563 open) is Algora's label applied to the real GitHub issue,
# which is how Algora's inventory is reachable for free. `bounty` (3825) is
# noisy and carries the floor and spam filters' weight. Tune this list against
# real digest output; it is here so that needs no code change.
BOUNTY_QUERIES = [
    'label:"💎 Bounty" state:open is:issue no:assignee -label:"💰 Rewarded"',
    'label:bounty state:open is:issue no:assignee',
]

BOUNTY_SEARCH_MAX_PAGES = int(os.environ.get("BOUNTY_SEARCH_MAX_PAGES", "10"))

# Markers meaning "this bounty has already paid out". Matched case-insensitively
# against labels, so entries here must be lowercase. Other platforms' markers are
# unknown; add them as they are observed.
PAID_LABELS = {
    label.strip().lower()
    for label in os.environ.get("PAID_LABELS", "💰 Rewarded").split(",")
    if label.strip()
}

# A bounty below this is not worth a working day. A MISSING amount does not
# reject -- it sorts last, because expected value cannot be computed without one.
AMOUNT_FLOOR_USD = int(os.environ.get("AMOUNT_FLOOR_USD", "50"))

# Expensify Help Wanted issues pay a flat rate; there is no $-label to parse.
EXPENSIFY_BOUNTY_USD = int(os.environ.get("EXPENSIFY_BOUNTY_USD", "250"))

DIGEST_TOP_N = int(os.environ.get("DIGEST_TOP_N", "25"))
DIGEST_STANDING_N = int(os.environ.get("DIGEST_STANDING_N", "5"))

# Two quota classes, shared with the Expensify watcher through one token. The
# budget guard in store.py refuses to start a run it cannot finish rather than
# reporting an empty world -- a throttled search is indistinguishable from "no
# results" (HANDOFF.md trap #3).
SEARCH_QUOTA_PER_MIN = int(os.environ.get("SEARCH_QUOTA_PER_MIN", "30"))
CORE_QUOTA_PER_HOUR = int(os.environ.get("CORE_QUOTA_PER_HOUR", "5000"))
CORE_BUDGET_PER_RUN = int(os.environ.get("CORE_BUDGET_PER_RUN", "1500"))

# How long a caller will block waiting for an exhausted quota window to roll
# over. Just over a minute, so a run that starts near the end of a search-quota
# window does not fail for a reason that resolves itself in seconds. The test
# suite zeroes this via an autouse fixture -- a suite that can sleep is a trap.
BUDGET_WAIT_S = float(os.environ.get("BUDGET_WAIT_S", "65"))

# Pause between consecutive search PAGES. Not about the documented 30/min pool --
# that is what the budget guard is for. This defends against GitHub's secondary
# (burst) rate limit, which fires on request rate regardless of remaining quota:
# measured 2026-08-08, ten back-to-back pages got a 403 with 23 of 30 primary
# units still unspent. At <=20 pages a run and a 3-hourly cadence, a couple of
# seconds a page is free; tripping the burst guard costs the entire run.
SEARCH_PAGE_PAUSE_S = float(os.environ.get("SEARCH_PAGE_PAUSE_S", "3"))

HUNT_ENABLED_SOURCES = [
    name.strip()
    for name in os.environ.get("HUNT_SOURCES", "github-labels,expensify").split(",")
    if name.strip()
]


def is_proposal(body: str | None) -> bool:
    """Exact reimplementation of getIsProposal() from proposalPoliceComment.ts:38."""
    if not body:
        return False
    lowered = body.lower()
    return PROPOSAL_KEYWORD in body and PROPOSAL_HEADER_A in lowered and PROPOSAL_HEADER_B in lowered


def is_bot_author(login: str | None) -> bool:
    return (login or "") in BOT_AUTHORS
