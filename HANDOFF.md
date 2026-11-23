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

## Which Expensify issues are worth a verified proposal (measured 2026-08-12)

Ran every open Help Wanted issue through the rejects. **75 open → 13 worth considering.**
Rejected: `crowded=40` (>6 rivals), `platform=13` (iOS/Android/Hybrid only), `not-a-bug=9`.
**`awarded=0` — none of the 75 had been given to anyone.**

Best targets, fewest rivals first: **#87373** (2 rivals, open since Apr 08), **#94981**
(3, Jun 30), #94559 (3), #86498 (4), #91006 (6), #92160 / #94728 / #97215 (5–6).
Filter out `[HOLD]` titles and #96139/#96140 (duplicate `Onyx.connect()` refactors — no
bug, so nothing to reproduce).

### Two populations, and only one of them is ours

Four issues hand-checked (#98107, #98001, #91581, #97949) all came back "skip", each for
a different reason, and together they define the targeting rule:

| population | win condition | our machinery |
| --- | --- | --- |
| obvious bugs / content tasks | be **first**; minutes matter | actively harmful — 30–60 min of latency |
| hard, long-open, multi-file bugs | be **right** | this is where verification pays |

#98107 ("Go back to home page" dead) is the cautionary one: a fallback-less
`Navigation.goBack()` at `ValidateCodeModal.tsx:49-51`, which ten people independently
found within three days. My analysis matched all ten — there was no differentiated
insight available, and posting would have been a near-certain ProposalPolice duplicate.
An issue open for four months with two proposals is the opposite bet and the one to take.

#98001 shows the other end: no `Bug` label, so nothing is broken, so there is no
reproduction and every stage of the loop is a no-op. Its "fix" was four supplied literals
appended to `CONST.HOME.ANNOUNCEMENTS`. Hard-skip non-`Bug` issues.

## OPEN ITEMS for whoever picks this up next

**1. `has_open_linked_pr()` is WRONG for Expensify, and it is silently costing bounties.**

`hunt.py` stage 3 rejects any candidate with an open cross-referenced PR. On Algora repos
that is correct -- the PR _is_ the claim. On Expensify it is not: selection happens by
**proposal**, a C+ picks one, and only then is that person assigned and writes the PR.
Plenty of people now run AI agents that auto-raise PRs on Help Wanted issues. Those PRs are
noise. Rejecting on them discards still-winnable $250 bounties as "taken", in the
highest-value channel available, without saying so.

Fix: make the filter source-aware -- skip it for `source == "expensify"`, or demote it to a
displayed signal rather than a reject. NOT DONE as of 2026-08-09.

**2. Rotate the GitHub PAT.** A failing pytest traceback printed it on 2026-08-08. Not in
git, not in any repo file, but present in three `.jsonl` transcripts under
`~/.claude/projects/`. Recurrence is prevented (`tests/conftest.py` seeds a dummy token
before `sniper.config` loads; `tests/test_token_guard.py` pins it) but the exposure stands.
Update `.env` on BOTH hosts.

**3. Nothing has survived to a notification yet.** Every link is proven end to end -- real
label events discovered, real filters applied, real enrichment, Discord HTTP 204 -- but no
candidate has passed every filter, because as of 2026-08-09 every live bounty at a proven
payer already had an open linked PR. That is a true finding, not a broken pipeline. Judge
liveness by `watch.log` growing on the box, never by Discord silence.

**4. If it stays quiet, widen rather than loosen.** The payer roster is capped at the 43
most-recent payers; there are ~3,100 rewarded issues of history to mine. Dropping the $50
floor is also reasonable -- a real $50 from a proven payer beats a fake $9,000.

## Bounty tracker (`hunt.py`)

Expensify is now one `Source` among several. `hunt.py` discovers open unclaimed
bounties from GitHub label search, rejects noise before spending quota on it,
ranks on metadata alone, and pushes a Discord digest every 3 hours from OCI.

| file                | does                                                                       |
| ------------------- | -------------------------------------------------------------------------- |
| `sniper/models.py`  | `Candidate`; amount parsing from label, then title, then body              |
| `sniper/filters.py` | free rejects (search payload) then repo rejects (needs enrichment)         |
| `sniper/score.py`   | `amount / (1 + comments) x repo_health`. Age and language are never scored |
| `sniper/sources/`   | `Source` protocol, bounty-label source, Expensify source                   |
| `sniper/notify.py`  | digest formatting, Discord webhook / Telegram                              |
| `hunt.py`           | the pipeline                                                               |

`python3 hunt.py --dry-run` prints without sending. 212 tests, no network.

### Traps this cost real time to learn

1. **GitHub has a SECONDARY rate limit** that fires on request bursts regardless
   of remaining quota -- a 403 arrived with 23 of 30 units unspent. The budget
   guard models the documented pools and structurally cannot see it.
2. **`per_page=100` trips it on the FIRST request** for this query, reproducibly;
   50 and 30 do not. GitHub weights by how expensive a query is to serve. At 100
   the tracker returns an empty digest on every run.
3. **Abandoning a throttled query costs most of the inventory.** A secondary
   limit clears in minutes, so pages are retried with doubling backoff. Giving up
   cost 420 of 520 candidates and produced a digest where 21 of 22 entries came
   from one repo.
4. **A throttle mid-search used to discard the pages already fetched**, so the
   digest reported an empty world after collecting 150 real candidates.
5. **Search result items carry no `repository` object**, only `repository_url` --
   so `fork`/`archived` cost a core call and cannot be free rejects.
6. **Search responses carry no ETag.** Incremental discovery diffs `updated_at`
   against SQLite instead.

### The market's actual shape (measured 2026-08-09) -- read this before tuning anything

The tracker works. The channel is the problem, and the numbers say why.

**Proven payers pay steadily but hold no standing inventory.**

    Algora issues ever rewarded                          3,114
    rewarded, updated in the last 90 days                  501
    rewarded, updated in the last 30 days                   10

    open UNASSIGNED bounties at the 10 most-recent payers    3
      (Mudlet, qdrant, projectdiscovery/httpx, CapSoftware,
       documenso, outerbase, tscircuit x2, gitea, highlight)

**Farms hold enormous inventory and pay nothing.**

    ClankerNation/OpenAgents      201 open, 0 rewarded, 0 closed
    UnsafeLabs/Bounty-Hunters     182 open, 0 rewarded, 0 closed

So visible inventory is INVERSELY correlated with inventory that pays. Anything
still sitting unassigned on a real payer's repo is sitting there for a reason;
everything else has been claimed within hours. A 3-hourly digest is the right
cadence for mining a backlog and the wrong one for catching real money.

**Expensify, for comparison:** 86 open Help Wanted, **0 unassigned**, 6,264
closed all time. Same shape -- a proven payer with zero standing inventory.

Implication: a periodic digest cannot win a bounty that is claimed in minutes.
The two ways forward are (a) poll the ~38 known payer repos frequently and act
fast, or (b) accept that this channel supplements rather than provides income.
Do not tune the filters expecting a bigger list -- the filters are not what is
limiting it.

### What the inventory actually looks like (measured 2026-08-09)

1506 candidates seen, 738 live, but only **206 carry a readable amount, across 5
repos**. Two of those five look like farms: `ClankerNation/OpenAgents` alone has
188 live issues totalling $917,000, and `UnsafeLabs/Bounty-Hunters` tags COBOL
work on a TypeScript repo. Scoring cannot tell a farm from a generous payer --
`DIGEST_MAX_PER_REPO` caps any one repo at 3 slots so the rest stay visible.

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

## Verify-loop results as of 2026-08-09 — READ BEFORE RESUMING THAT WORK

**Answered:** yes, generated Playwright steps CAN reproduce bugs in Expensify's UI. Two
runs reproduced real bugs and two more got to 25/26 and 18/19 steps.

**Not answered, and it is the whole question: the loop has emitted ZERO proposals.**
Both completed runs were refuted at the last gate.

| run                   | cost              | outcome                                                        |
| --------------------- | ----------------- | -------------------------------------------------------------- |
| #96348                | $9.21             | reproduced → fixed → symptom gone → test written → **REFUTED** |
| #96352                | $26.05            | same, two-user flow via `as_user` → **REFUTED**                |
| #95711 (ground truth) | ~$16 over retries | never reproduced; died at 25/26 steps                          |
| #95149 (ground truth) | in flight         | —                                                              |

Both refutations were verified by hand against spec and code and were **correct**:

- #96348 — `contributingGuides/MENTIONS_HIGHLIGHTING_IN_CHAT.md:8,13` _specifies_ the
  behaviour. Our account is on a public domain (gmail), so
  `areEmailsFromSamePrivateDomain` (`src/libs/LoginUtils.ts:84-86`) means it has no
  short-mention namespace at all — the bug is unreproducible with it in principle.
- #96352 — the repro clicked `DisplayNamesWithToolTip` (the header _title_), not the
  "From" link, and blamed the element it never touched.
  `AvatarWithDisplayName.tsx:381-390` wraps the entire header in one pressable.

**The pattern that matters: both refutations killed the REPRODUCTION, not the theory.**
The discovery agent authors both the repro and the theory, so they agree by construction.
A repro written to confirm a belief is not evidence of it. Before implementing, confirm
the repro exercised the exact element/branch being blamed and that a CONTROL element does
not behave the same way.

**Open calibration risk:** at n=2 a 100% rejection rate cannot be distinguished from a
refuter that rejects everything. Both rejections were right, so it is not miscalibrated
yet — but a refuter that never passes is an expensive way to produce nothing. Track false
rejections as runs accumulate.

### Ground-truth harness (`groundtruth.py`)

Scores the loop against issues whose merged fix is already known — the only test the
loop's own internal consistency cannot fool.

- Finds the fix PR by the `$ https://github.com/Expensify/App/issues/<n>` line in the PR
  body. **Do not take the first merged cross-referenced PR**: for #95711 that picked
  #95318, the PR that _introduced_ the regression, which would have inverted the score.
- Checks out just before the fix **merged** (not before the winning proposal was posted).
  Pre-merge commits are ~180 package-lock lines from HEAD so they share `node_modules`;
  the proposal cutoff can be weeks earlier and 13k lines away.
- Hides hindsight separately via `verify.py --as-of <cutoff>`, which drops every comment
  from the winning proposal onward.
- `--reuse-steps` skips the discovery stage (20–46 min, $3.50–18) on a retry.
- Waits for a real HTTP 200 from the dev server before driving the browser — the port
  opens before rsbuild finishes compiling.

### Operational constraints learned the hard way

- **Anthropic session limits kill a run mid-flight** and are NOT retryable (unlike the
  safety classifier, which is — `SafeguardFlagged` retries 3×). A full run needs a long
  discovery stage plus five more model calls, so any scheduler must back off and resume
  rather than mark the issue failed.
- Cost per complete run: **$9–26**, dominated by discovery.
- Isolation (`SNIPER_ISOLATE=1`) is for backtests only. Production runs un-isolated so
  the child sees `CLAUDE.md` and the repo skills. Every number above the "Measurements"
  heading was taken WITH isolation on and on the CLI's default model — they are floors.
  `SNIPER_MODEL` now pins the model explicitly.

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
- cron/systemd scheduling on OCI for `verify.py` (the bounty tracker `hunt.py`
  is already scheduled: `deploy/install-hunt-cron.sh`, every 3 hours)
- `AUTO_POST` default is `1` in config but nothing posts yet

## claude-omni (routing generator calls through OmniRoute)

`~/.local/bin/claude-omni` is Claude Code pointed at a local OmniRoute
(`http://127.0.0.1:20128`, which serves `/v1/messages` in Anthropic format). Plain
`claude` is untouched — the wrapper sets `ANTHROPIC_BASE_URL` only for the process it
execs, uses its own `CLAUDE_CONFIG_DIR` (`~/.claude-omni`), and **unsets
`CLAUDE_CODE_OAUTH_TOKEN`** (otherwise that wins and traffic quietly goes to Anthropic
anyway, which looks like success while defeating the point).

**The trap it guards:** on 2026-08-08, `auto/claude-opus` returned HTTP 200 with a
well-formed reply served by `gemini-3.1-flash-lite`; `auto/claude-sonnet` served
`gemini-2.5-flash`. No Anthropic provider was reachable (kilocode 401, cursor 400,
openrouter 403). For work whose value IS analysis quality, that downgrade is invisible —
it shows up as worse proposals, never as an error. The wrapper therefore probes first,
prints `requested X -> served by Y`, and refuses a Claude→non-Claude substitution unless
`OMNI_ALLOW_DOWNGRADE=1`.

The operator reports having fixed provider auth on 2026-08-09. **Re-probe rather than
assuming either state**, and never trust an `auto/*` alias to honour the vendor in its
name — check the `model` field of the response.

Two things to settle before routing the pipeline through it:

1. **OmniRoute runs on the LOCAL machine; the sniper runs on OCI.** OCI cannot reach
   `127.0.0.1:20128` without a reverse SSH tunnel or its own OmniRoute install.
2. **Change one variable at a time.** Every measurement in this document was taken on
   Claude. Swapping the model _and_ the harness together makes any quality change
   unattributable. Get a ground-truth number on Claude first, then re-run the same issue
   through `claude-omni` and compare directly.

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
