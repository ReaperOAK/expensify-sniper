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


def is_proposal(body: str | None) -> bool:
    """Exact reimplementation of getIsProposal() from proposalPoliceComment.ts:38."""
    if not body:
        return False
    lowered = body.lower()
    return PROPOSAL_KEYWORD in body and PROPOSAL_HEADER_A in lowered and PROPOSAL_HEADER_B in lowered


def is_bot_author(login: str | None) -> bool:
    return (login or "") in BOT_AUTHORS
