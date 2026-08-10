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

# The board lives in ONE private gist so the operator has a stable bookmark.
# Create it once with `python3 hunt.py --init-gist` and paste the id here.
BOUNTY_GIST_ID = os.environ.get("BOUNTY_GIST_ID", "").strip()

# Writing a gist needs the `gist` scope; reading public bounties needs none at
# all. Measured 2026-08-10: GITHUB_TOKEN here is a classic PAT with an EMPTY
# scope list, which is exactly right for the rest of this tool -- it can read
# every public issue and can do nothing else, so a leak costs nothing. Creating
# or updating a gist with it returns 404 (GitHub reports a missing scope as
# not-found, not as forbidden, which reads as "your gist is gone").
#
# So the gist credential is separate rather than upgrading the read token's
# scopes. Least privilege: one narrow token that can write one gist, and a read
# token that stays powerless. Falls back to GITHUB_TOKEN so a single
# sufficiently-scoped token also works.
BOUNTY_GIST_TOKEN = (os.environ.get("BOUNTY_GIST_TOKEN", "").strip()
                     or GITHUB_TOKEN)
BOARD_PATH = Path(os.environ.get("BOARD_PATH", ROOT / "bounties.md"))
# How many amountless candidates the board lists. 934 once the 446 'open linked
# PR' rows are revived, so this is a page-size guard that DOES bite -- the
# section heading says "showing 600 of 934" when it does, and the signature the
# change detector stores carries the true count rather than the page length.
BOARD_UNRANKABLE_N = int(os.environ.get("BOARD_UNRANKABLE_N", "600"))
# The board's own rankable page size, deliberately not DIGEST_TOP_N (25). The
# digest is a shopping list and 25 is a reader's attention budget; the board is
# the inventory, and there were 48 priced live rows post-revive. Borrowing the
# digest's number printed "Rankable — 25" over an inventory of 48.
BOARD_RANKABLE_N = int(os.environ.get("BOARD_RANKABLE_N", "200"))
# How many example issues each reject reason carries on the board. A count says
# a filter is big; an example says it is wrong, which is the half that caught
# 'open linked PR = 562'.
BOARD_REJECT_EXAMPLES = int(os.environ.get("BOARD_REJECT_EXAMPLES", "3"))
# Percentage move in the unrankable count that is worth a Discord link.
BOARD_CHANGE_PCT = float(os.environ.get("BOARD_CHANGE_PCT", "5"))

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

# 20, not 10: SEARCH_PER_PAGE halved, so twice the pages are needed to reach the
# same 1000-result ceiling the Search API imposes.
BOUNTY_SEARCH_MAX_PAGES = int(os.environ.get("BOUNTY_SEARCH_MAX_PAGES", "20"))

# 50, not the API maximum of 100. Measured 2026-08-08 against the live endpoint:
# per_page=100 on the bounty query returned 403 "secondary rate limit" on the
# FIRST request with 24 of 30 primary units unspent, reproducibly, while
# per_page=50 and per_page=30 both returned 200 immediately afterwards. GitHub's
# burst guard weights by how expensive a query is to serve, and a 100-result page
# over a label query with a negation is over that line. This is not a tuning
# preference -- at 100 the tracker returns an empty digest every run.
SEARCH_PER_PAGE = int(os.environ.get("SEARCH_PER_PAGE", "50"))

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

# Cap per repository in the digest. Measured 2026-08-09: one repo held 188 live
# issues worth \,000 and took all 25 slots, burying 26 other repos. Whether
# that repo is generous or a bounty farm is not a judgement metadata can make --
# but a digest that only ever shows one repo is useless either way.
DIGEST_MAX_PER_REPO = int(os.environ.get("DIGEST_MAX_PER_REPO", "3"))

# Payout history -- the strongest signal available, and the one the tracker
# originally ignored. Measured 2026-08-09: two repos advertising $917,000 across
# 383 open Algora bounties had never rewarded or even closed a single one, while
# a repo offering $200 had paid out 150 times. Amount alone ranks them backwards.
# Cached for a day: payout history moves in days, not in the 3h between runs.
PAYER_CACHE_S = float(os.environ.get("PAYER_CACHE_S", str(24 * 3600)))
PAYER_SEARCH_MAX_PAGES = int(os.environ.get("PAYER_SEARCH_MAX_PAGES", "6"))
# How many open Algora bounties a repo must be advertising before never having
# paid one counts against it. A new project with two bounties is not a farm.
UNPAID_REPO_THRESHOLD = int(os.environ.get("UNPAID_REPO_THRESHOLD", "10"))

# Closing an issue is not paying it -- a farm can close its own issues just as
# freely as a real programme does. Measured 2026-08-10 with `gh api
# search/issues`, closed-with-label count vs. closed-AND-`linked:pr` count:
#
#   repo                                            closed  +linked:pr  ratio
#   zhangjiayang6835-cyber/bounty-plaza (farm)          198          23    12%
#   activepieces/activepieces (proven payer)            173         170    98%
#   commaai/opendbc (own programme, no Algora label)      7           4    57%
#
# bounty-plaza's raw closed count (198) held three of the top five digest
# slots under the old ">0 ever closed" rule alone. Sampling #12, #13 and #16
# found them closed 2026-07-12T10:43:51Z / :55Z / :58Z -- seven seconds apart,
# none assigned, one titled "[Bounty] [bounty][funding-pending]: complete the
# first permi...": a bulk close, not payout history. A real programme closes
# an issue BECAUSE work merged, so its closures carry linked PRs. 0.34 sits
# comfortably below opendbc's 57% and well above the farm's 12%; activepieces'
# 98% is nowhere near either edge.
#
# `-no:assignee` is silently ignored by GitHub's search API -- measured
# 2026-08-10, it returned the identical 198 for the farm, whose issues have no
# assignee -- so it is not part of either query. `linked:pr` does work.
CLOSED_PR_RATIO_FLOOR = float(os.environ.get("CLOSED_PR_RATIO_FLOOR", "0.34"))

# A ratio computed on a handful of closures is not evidence, it is noise that
# happens to look like evidence. Measured 2026-08-10, SecureBananaLabs/
# bug-bounty had closed exactly ONE 💎 Bounty issue, with a linked PR -- 1/1 =
# 100%, sailing straight past CLOSED_PR_RATIO_FLOOR while the repo had 0
# issues ever marked 💰 Rewarded and 31 other open bounties sitting unclaimed.
# Below this many closed issues, payout_reject abstains on the ratio rather
# than trusting it. `closed.total == 0` is untouched by this floor and keeps
# rejecting outright -- "never closed one" needs no sample size to be
# damning, only the RATIO does. commaai/opendbc (7 closed, 4 with PR, 57%) is
# the repo this floor must never catch: getting the direction backwards
# (reject-below instead of abstain-below) would kill a genuine payer for
# having too small a track record, which is exactly backwards.
CLOSED_SAMPLE_MIN = int(os.environ.get("CLOSED_SAMPLE_MIN", "5"))

# --- payer watch --------------------------------------------------------------
# How many proven-payer repos to watch. Each costs one core request per sweep,
# and nothing at all when the repo is unchanged (a 304 leaves the quota
# untouched, measured 2026-08-09). 38 distinct payers appeared in the 100 most
# recent payouts, so this covers the live set with room to grow.
WATCH_MAX_REPOS = int(os.environ.get("WATCH_MAX_REPOS", "60"))
# Sweep cadence, minutes. Not a preference: a single events page holds 100
# entries and covered only ONE DAY on go-gitea/gitea, so polling slower than a
# repo's event velocity loses labels silently. Ten minutes is far inside that
# for every payer measured.
WATCH_EVERY_MINUTES = int(os.environ.get("WATCH_EVERY_MINUTES", "10"))

# Which sources the fast sweep runs. Expensify belongs here, not in the 3-hourly
# digest: it is the best-proven payer available (6,264 closed Help Wanted at a
# flat $250) and its issues are claimed fast -- 86 open, ZERO unassigned,
# measured 2026-08-09. Its 100-event page spanned 5h18m that day, so a 3-hourly
# poll carried only 1.8x margin against a busy spell while a 10-minute one
# carries 32x. The original sniper polled it every 20 SECONDS for this reason.
WATCH_SOURCES = [
    name.strip()
    for name in os.environ.get("WATCH_SOURCES", "payer-watch,expensify").split(",")
    if name.strip()
]

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

# Same burst guard, core side. Run 1 is a backlog dump: one repo-detail call per
# unique repo plus one timeline call per survivor, which is hundreds of requests
# back to back. The 5000/hr pool has room for that; the burst guard does not.
# Smaller than the search pause because core requests are individually cheap.
CORE_CALL_PAUSE_S = float(os.environ.get("CORE_CALL_PAUSE_S", "0.5"))

# How many 100-event pages of one issue's timeline stage 3 will read.
#
# Measured 2026-08-10 on the live box: with no bound at all, ONE candidate
# consumed 75 consecutive core calls -- 7,500+ timeline events on a single
# issue -- while the sweep did not advance by a single row. Stage 3 fell to
# about three candidates a minute, which over 975 survivors is longer than the
# interval of the cron that calls it.
#
# The predecessor of linked_pr_state could not hit this because it stopped at
# the first open PR and almost never left page one. Counting removed that stop
# and nothing replaced the bound.
#
# 3 pages is 300 events, which covers every real bounty issue; anything past it
# is pathological. Truncating is safe in BOTH directions, which is why a cap is
# acceptable here at all: a claimant found is positive evidence and still
# rejects, and a claimant not found leaves the candidate live -- the safe error,
# since a kept candidate gets read by a human and a rejected one disappears.
TIMELINE_MAX_PAGES = int(os.environ.get("TIMELINE_MAX_PAGES", "3"))

# A secondary (burst) throttle clears in minutes, so a search page is worth
# waiting out rather than abandoning: measured 2026-08-09, giving up on one cost
# 420 of 520 candidates and produced a digest dominated by whichever repo filled
# page one. Backoff doubles per attempt (60s, 120s, 240s). Only SECONDARY limits
# are retried -- a primary one needs the whole window to roll.
SEARCH_MAX_RETRIES = int(os.environ.get("SEARCH_MAX_RETRIES", "3"))
SEARCH_RETRY_BACKOFF_S = float(os.environ.get("SEARCH_RETRY_BACKOFF_S", "60"))

HUNT_ENABLED_SOURCES = [
    name.strip()
    for name in os.environ.get("HUNT_SOURCES", "github-labels,expensify").split(",")
    if name.strip()
]

# Sources where an assignee tells us nothing about whether the work is taken.
#
# Expensify assigns an internal engineer the moment a job is posted, and a
# Contributor+ when one volunteers. Verified 2026-08-09: #97949 was open and
# accepting proposals, labelled External/Daily/Help Wanted, assigned to
# stitesExpensify. The blanket assignee reject therefore dropped 100% of
# Expensify candidates at stage 1 -- the whole $250 channel, silently.
#
# Whether such a job has actually been awarded is answered in stage 3 instead:
# once a C+ picks a proposal, its author is assigned AND opens the PR, which is
# exactly what github_api.linked_pr_state calls a claim.
# Lowercased, and matched against a lowercased source name, exactly as
# PAID_LABELS is three sections above. Without it `ASSIGNEE_NOT_A_CLAIM=Expensify`
# in .env -- the obvious spelling, since that is how the project is written --
# exempts nothing at all, the blanket assignee reject drops 100% of Expensify
# candidates at stage 1 again, and the $250 channel goes dark for the second
# time with no error anywhere. Silent-no-match is the failure this whole setting
# exists to undo.
ASSIGNEE_NOT_A_CLAIM = frozenset(
    name.strip().lower()
    for name in os.environ.get("ASSIGNEE_NOT_A_CLAIM", "expensify").split(",")
    if name.strip()
)


def is_proposal(body: str | None) -> bool:
    """Exact reimplementation of getIsProposal() from proposalPoliceComment.ts:38."""
    if not body:
        return False
    lowered = body.lower()
    return PROPOSAL_KEYWORD in body and PROPOSAL_HEADER_A in lowered and PROPOSAL_HEADER_B in lowered


def is_bot_author(login: str | None) -> bool:
    return (login or "") in BOT_AUTHORS
