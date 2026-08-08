# expensify-sniper — system handoff

Automates competing for Expensify Help Wanted bounties (~$250/issue) as GitHub user
`ReaperOAK`. Expensify's flow is: issue gets `Help Wanted` → contributors post proposals
→ a Contributor+ (C+) picks one → that person is assigned and writes the PR. So the
deliverable is a **proposal**, never an unsolicited PR.

## Goal and the governing rule

> "you should test and verify everything first. I mean if we aint generating winning
> proposals then this is all a waste"

Measure before building. Two measurements have already invalidated earlier work; assume
the next one will too.

## Where it runs

|       | local (`~/Documents/App`, `~/Documents/expensify-sniper`) | OCI (`ssh openclaw`)                   |
| ----- | --------------------------------------------------------- | -------------------------------------- |
| role  | build + debug                                             | production, always-on                  |
| specs | 16 cores, 23GB RAM, ~21GB disk free                       | aarch64, 4 cores, 23GB RAM, 145GB free |
| state | `npm ci` still finishing                                  | **fully bootstrapped and verified**    |

OCI is the deployment target. Repo at `~/App`, sniper at `~/expensify-sniper`.

## Components

| file                 | does                                                              |
| -------------------- | ----------------------------------------------------------------- |
| `sniper/config.py`   | loads `.env`, ProposalPolice constants, `is_proposal()`           |
| `sniper/mailbox.py`  | Gmail IMAP magic-code retrieval (Expensify login is passwordless) |
| `sniper/repro.py`    | Playwright driver: login + step verbs + evidence capture          |
| `verify.py`          | **the loop**: reproduce → implement → re-verify → test → propose  |
| `backtest.py`        | scores generated proposals against known merged fix PRs           |
| `melvin_accuracy.py` | scores MelvinBot's analysis with the same metric                  |
| `appenv.sh`          | selects node 26.5.0 + bun; source before any npm command          |

## The loop (`verify.py <issue>`)

1. **context** — issue body + MelvinBot analysis + rival proposals from GitHub
2. **discover** — an agent drives a REAL headed browser (Playwright MCP, pre-authenticated
   via saved storage state, under Xvfb), reproduces the bug itself, then emits a
   deterministic step list whose LAST step is an assertion that fails while the bug is present
3. **reproduce** — replay that list on the local dev server; if it does not reproduce, **abort**
4. **implement** — `claude -p` with Edit/Write patches `src/`, with repro evidence in context
5. **re-verify** — replay the IDENTICAL steps; if the bug persists, **abort** (fix is wrong)
6. **test** — write + run a Jest regression test
7. **refute** — revert the fix, then a separate agent tries to KILL the root cause.
   Refuted → **abort**. Only a claim that survives gets proposed.
8. **propose** — emit a proposal claiming only what was verified
9. **restore** — `git checkout -- src tests assets`; never `git clean`, so untracked files survive

Aborting instead of proposing is the point. An unfalsified proposal is what this exists
to prevent.

### Why discovery is agentic but replay is static

An agent exploring live gets selectors right that source-reading cannot: one static run
died on `testid=composer`, which is `CONST.COMPOSER.NATIVE_ID` — an `id` attribute, not a
testID (the real one is `reportActionCompose`).

But the step list it emits is then **frozen**. Before/after is only evidence if both runs
execute identical steps; an agent improvising again after the fix could take a different
path, or route around the bug and declare it fixed. Adaptive discovery, reproducible
verification.

### Making the generator good

- **Do not isolate in production.** `CLAUDE_CONFIG_DIR` isolation exists only to stop the
  `remember` hook leaking prior analysis into _backtests_. It also strips `CLAUDE.md` and
  every skill, so the child loses the App architecture notes, Onyx docs, style guide and
  contributingGuides. On a live issue there is nothing to leak. Controlled by
  `SNIPER_ISOLATE=1` (default off). **Every measurement in this document was taken with
  isolation ON, i.e. on a handicapped model — treat those numbers as a floor.**
- **`TRAPS.md`** is injected into every prompt and appended on each failed run. It is the
  loop's memory; read it before assuming a failure is novel.
- **The refute stage runs on a reverted tree** so it judges the claim against shipping
  code rather than being persuaded by a diff that already assumes the claim.

## Multi-account (the coverage unlock)

Most Expensify issues say "Login as User A ... then as User B". That is NOT a blocker.
Every `oaak78692+tag@gmail.com` alias is a distinct Expensify account that delivers to
the SAME mailbox, so one IMAP login drives any number of users.

- `as_user("b")` creates and signs in a fresh account in its own browser context, and
  switching back to a name resumes that session.
- Its address is the primary with the name appended to the +tag:
  `x+sniper@gmail.com` -> `x+sniperb@gmail.com`.
- Magic codes MUST be filtered by recipient (`Delivered-To` keeps the +tag) or one
  user's login consumes another's code.
- First login for a new alias is slow — the account is created before the code is
  mailed. `CODE_TIMEOUT_S` is 300 for that reason.

Remaining genuine blockers: Expensify Classic (OldDot), paid plan tiers, card/bank
fixtures, mobile devices, and private email domains (short mentions need one; ours is
public). `triage.py` encodes these and ranks open issues by reproducibility then rival
count.

## What is proven

- Gmail IMAP magic-code retrieval — works (codes land in **Spam**; server-side `FROM` filter
  is required, the mailbox has ~37k messages)
- Login end-to-end on OCI against the local dev server — `ok: true`, fully unattended
- NewDot builds on ARM64 in **51.9s**; dev server serves `https://dev.new.expensify.com:8082`
- Cloudflare: **headed passes, headless is blocked** (replicated both directions —
  headless gets `403` on `/api/Ping` + "You appear to be offline"). **Xvfb is mandatory.**
- Test account onboarding completed, auth state cached per-host

## What is NOT proven — the open question

**Can generated Playwright steps actually reproduce a bug in Expensify's UI?**
Zero loop runs so far. React Native Web means class names are generated and unstable, so
selectors must be testID / aria-label / visible text. If this leg is unreliable the whole
verified-proposal thesis collapses to static analysis.

Next action: `xvfb-run -a python3 verify.py 96348` on OCI.
(#96348 = "@j.doe short-email mention not recognized" — chosen because the repro is
typing one string in a chat, no backend setup. It has 7 rival proposals, so it is a
machinery test, not a real bid.)

## Measurements (do not re-derive)

**Static-analysis-only generator, 4 closed issues, contamination-free:**

- file-hit rate mean **0.855** — but that metric lies
- mechanism vs merged diff: **1 clear win, 2 partial, 1 loss**
- #95711 scored 0.75 on files while being completely wrong, and explicitly ruled out the
  correct answer by name

**MelvinBot's own analysis, 29 closed issues, same metric, 0 errors:**

- mean **0.52**, median 0.33, perfect 9/29, zero 3/29
- decays with fix size: 1 file 1.00 → 2-3 files 0.59 → 4-9 files 0.47 → 10+ files 0.31
  (that metric is structurally biased against large PRs, so the last bucket overstates it)

Conclusion: MelvinBot is a lead, not a seed. Static analysis alone is ~1-in-4.

## Traps that have already caused wrong results

1. **Memory contamination.** The user-level `remember` SessionStart hook injects
   `~/Documents/App/.remember/` into every `claude -p` child by absolute path regardless
   of cwd. It leaked prior analysis of the exact issues being benchmarked. Fix:
   `CLAUDE_CONFIG_DIR=<isolated dir>` in the subprocess env, plus
   `CLAUDE_CODE_OAUTH_TOKEN` (the isolated dir has no credentials). `--bare` also strips
   hooks but forces `ANTHROPIC_API_KEY` and loses Max-plan billing.
2. **Usage limits return as valid JSON.** A limit notice arrives as a well-formed payload
   whose `result` is just the notice, and scores as a legitimate zero. Guard on length +
   "session limit" / "usage limit".
3. **GitHub search API is 30 req/min.** A rate-limited search looks identical to "no
   results". Use the issue **timeline** endpoint (5000/hr core limit) for linked PRs, and
   never let a helper return `None` for both "error" and "absent".
4. **Do NOT create a `.env` in the App repo.** `contributingGuides/SETUP_WEB.md` advises
   against it; a stale one is the documented cause of CORS errors. `npm run web` already
   starts the proxy that forwards `/api` to production.
5. **engine-strict.** bun 1.3.14 / node 26.5.0 / npm 11.17.0 exactly. bun lives under the
   node 24 prefix — symlink it into node 26's bin; putting node 24's bin on PATH shadows
   node 26 and the install fails with EBADENGINE.
6. **`_record()` converts `AssertionError` to `ReproError`.** Detect a failed assertion by
   inspecting the last recorded step, not by exception type.
7. **A fresh account is trapped in onboarding**, and the modal overlays the app so every
   element resolves but is never actionable — `count()` is 1 while `click()` times out.
   Reads exactly like a bad selector; it is not. Fix is `SKIP_ONBOARDING=true` in a
   **minimal `.env` file** in the App repo (a process env var does NOT work —
   react-native-config bakes values from `.env` at build time). Keep the file to that one
   key so everything else stays at its `src/CONFIG.ts` default. Clicking through
   onboarding does not work: "Continue" is inert and fires no API call at all.
8. **`claude` is not on PATH for non-interactive ssh or cron** — it lives under the nvm
   node prefix. Resolve it absolutely, and put node's bin on the child's PATH so its Bash
   tool can run `npm test`.
9. **Playwright MCP will try to install `chrome-for-testing`** and then stop and ask for
   approval, which a headless run can never give. Pass `--executable-path` pointing at the
   already-installed `chromium-*/chrome-linux/chrome` (NOT `chromium_headless_shell-*`).

## Process facts that shape strategy

- `contributingGuides/HOW_TO_WORK_WITH_MELVINBOT.md` — MelvinBot posts the **first**
  proposal on most jobs and the default path is a C+ implementing _its_ proposal. A
  contributor only wins with a **"meaningfully different"** approach.
- `contributingGuides/AI_ETIQUETTE.md` — "You Own What You Post"; submitting AI-generated
  code you have not tested is listed as a Don't; providing evidence of manual testing is a
  Do. The loop satisfies the substance; fully-auto posting with no human read does not.
- ProposalPolice auto-withdraws at **≥90% similarity** to an earlier proposal, only on
  `created` events (edits are exempt). `SIMILARITY_ABORT=80` is the safety margin.
- Median ~8.2 proposals per issue. `MAX_EXISTING_PROPOSALS=6` — later entries are lossy.

## Economics

- static proposal ≈ $1–3; full loop ≈ $10–20 and 30–60 min, **serial** (one dev server,
  one working tree, one browser) → roughly 1 issue/hour
- at $250/win: 25% win ≈ $60/win (good), 10% ≈ $150/win (thin), 5% underwater
- "every Help Wanted issue" is incompatible with the loop. Intended architecture: cheap
  static pass to rank candidates, expensive loop only on the best few — targeting issues
  where MelvinBot is likely wrong (multi-file fixes, where it drops to 0.31).

## Not yet built

- `sniper/similarity.py` (pre-post duplicate check against ProposalPolice)
- auto-post step
- cron/systemd scheduling on OCI
- `AUTO_POST` default is `1` in config but nothing posts yet

## Running things

```bash
# local: always source this before npm/node in the App repo
source ~/Documents/expensify-sniper/appenv.sh

# OCI: dev server (already running; BROWSER=none causes a harmless "spawn none ENOENT")
ssh openclaw 'cd ~/App && . ~/.nvm/nvm.sh && nvm use 26.5.0 && npm run web'

# OCI: anything driving a browser MUST be headed under Xvfb
ssh openclaw 'export PATH=$HOME/.local/bin:$PATH; cd ~/expensify-sniper && xvfb-run -a python3 verify.py 96348'
```

Secrets live only in `~/expensify-sniper/.env` (chmod 600, both hosts, outside any git
repo). Never echo them.
