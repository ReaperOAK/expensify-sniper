# Master prompt — continue bounty tracker work

Paste everything below the line into a fresh session.

---

Continue work on the multi-source bounty tracker. Design is already approved; the next step
is the implementation plan, not more design.

## Read first, in this order

1. `~/Documents/ReaperOAK/docs/superpowers/specs/2026-08-05-multi-source-bounty-tracker-design.md`
   — the approved spec (committed as `f83b953`). This is authoritative.
2. `~/Documents/expensify-sniper/HANDOFF.md` — the existing Expensify system, its measured
   results, and six traps that have already caused wrong results. Trap #3 (a rate-limited
   GitHub search is indistinguishable from "no results") shapes the tracker's error handling.
3. `~/Documents/expensify-sniper/sniper/github_api.py` and `sniper/store.py` — the two files
   being generalized.

## What was decided (do not re-litigate)

- **Approach C**: generalize `expensify-sniper` into a multi-source platform. Expensify
  becomes one `Source` among several, rather than *the* repo. Approaches A (separate package)
  and B (standalone repo) were considered and rejected.
- **Metadata-only ranking.** No LLM triage stage. Deliberate.
- **No stack matching.** Language is displayed as a column, never filtered or scored on.
- **Discord webhook** for the digest. `config.py` already declares `DISCORD_WEBHOOK`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — no new config plumbing needed.
- **Cron on the OCI box** (`ssh openclaw`), every 3 hours.
- **$50 amount floor**, configurable.
- **Age is displayed but never scored** — see the funnel evidence in the spec for why.
- `verify.py` behavior must be **identical** before and after. It is not in scope to change.

## Hard gate before any git command

`~/Documents/expensify-sniper` is **not a git repository** and has **zero tests**.

Stage 0 starts with `git init`. Before the first `git add`, extend `.gitignore` to cover:

- `.auth-state.json` — cached Playwright session cookies
- `.claude-clean/` — contains `.credentials.json` (Claude OAuth credentials)
- `*.jsonl`, `*.log`

The current `.gitignore` covers `.env`, `*.sqlite`, `*.sqlite-journal`, `corpus/`, `runs/`,
`evidence/`, `__pycache__/`, `*.pyc`, `.venv/` — but **not** the two credential files above.
A naive `git add -A` writes live credentials into history permanently.

## Immediate next step

Invoke the `superpowers:writing-plans` skill to turn the spec into an implementation plan.

Scope note flagged during design and still unresolved: stage 0 + the `github_api.py` refactor
+ the new tracker modules + cron deployment may be too much for one plan. Consider splitting
into two: (1) make the refactor safe and do it, (2) build the tracker on top. Decide this at
plan-writing time.

## Key evidence, so it isn't re-derived

Measured 2026-08-02 to 2026-08-05 against live APIs:

- GitHub Search API is the only viable source. Algora's REST API returns
  `{"errors":{"detail":"Not Acceptable"}}` and its `/bounties` page is a Phoenix LiveView
  shell with no amounts in served HTML. Polar sunset bounties entirely (126 API paths, zero
  bounty-related).
- Algora applies its `💎 Bounty` label to the real GitHub issue, so Algora inventory is
  reachable through GitHub Search for free.
- Funnel: `💎 Bounty` open = 563 → no assignee = 548 → not `💰 Rewarded` = 521.
- `created:` is the wrong axis (only 1 issue created in the last 30 days against 521 live
  candidates) — bounty labels land on old issues. Discovery must sort by `updated` and diff
  against SQLite.
- Noise fixtures to write regression tests against: `SecureBananaLabs/bug-bounty` (one issue
  carrying `$780`, `$1k`, and `$1.2k` labels at once — spam farm) and
  `WillSmithTE/qdrant-qdrant` (a fork, already `💰 Rewarded`).
- Quotas: search 30/min, core 5000/hr, `304` costs zero quota.

## Context worth carrying

Bounties are **active** income and are being run for near-term cash (~$200 target), not as a
passive play. Separate passive-income work exists (egg-rate site, blog) and is not part of
this project.
