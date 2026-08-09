# Tracker: fix the filters that discard money, and expose the inventory

Date: 2026-08-09
Status: approved (design), not yet implemented

## Why

The tracker runs, sweeps, and delivers. What it delivers is wrong in three ways, all
measured on the live OCI database on 2026-08-09.

    github-labels | open linked PR                       | 562
    github-labels | KEPT                                 | 536
    github-labels | no payout history (188 open, 0 paid) | 188
    github-labels | below $50 floor                      | 174

1. **`open linked PR` is the largest single reject bucket** — larger than everything
   kept. Whatever fraction of those 562 is wrong is the tracker's biggest loss.

2. **Every Expensify candidate dies at stage 1.** `filters.free_reject` rejects any
   issue with an assignee. Expensify assigns an internal engineer the moment a job is
   posted: `#97949` is open and accepting proposals right now, with labels
   `External, Daily, Help Wanted` and assignee `stitesExpensify`. So the $250 channel
   is structurally dark. It has not shown up yet only because the cron is hours old
   and `Help Wanted` lands about twice a day.

3. **A farm holds three of the top four digest slots.**
   `zhangjiayang6835-cyber/bounty-plaza`: created 2026-07-08, 5 stars, 526 open
   issues, `💎 Bounty` count 0, `💎 Rewarded` count 0. It advertises "Add a
   shrimp-person species 🦐" at $200. `payers.payout_reject` cannot see it because
   that rule only fires on Algora's own label, and this farm rolls its own.

And the digest's own footer names a fourth problem the operator cannot act on:

    529 more open and unclaimed, but with no amount readable from labels, title or
    body -- not rankable, so not shown.

Those 529 are invisible. There is no way to look at them.

## Non-goals

- Wiring the tracker to `verify.py`. The loop has emitted zero proposals in two
  completed runs at $9–26 each; spending on volume before that is resolved is
  backwards. The tracker surfaces Expensify candidates and the operator decides.
- Tuning `AMOUNT_FLOOR_USD`, `DIGEST_TOP_N`, or the scoring bands. The filters are
  what limit this, not the thresholds.
- Rotating the leaked PAT. Tracked separately in HANDOFF.md.

## A — Farm rejection: label-agnostic, plus a quality downrank

### A1. Reject on closed-bounty history, not on Algora's label

`payers.payout_reject` currently abstains unless the candidate carries the exact
string `💎 Bounty`. That gate exists for a good reason — a repo that never adopted
Algora's label has no payout record to be absent — but it means any farm using its own
label is invisible. Replace the label gate with direct evidence.

New helpers in `sniper/payers.py`:

```python
def bounty_label_of(labels: tuple[str, ...]) -> str | None:
    """The label to test this repo's payout history against, or None.

    Prefers Algora's own label, then any label containing "bounty"
    (case-insensitive), then any label that parses as an amount. None means the
    candidate carries nothing that marks it as a bounty at all, and the rule
    abstains — a repo's silence about a label it does not use says nothing.
    """

def ever_closed_with(conn, repo: str, label: str) -> int | None:
    """How many issues this repo has ever CLOSED carrying `label`.

    None means the question could not be answered (throttled, error). The caller
    must treat None as abstain, never as zero: "I could not look" and "there are
    none" are the distinction this whole pipeline is built against.
    """
```

`payout_reject` keeps its signature and gains a `closed_count: int | None` parameter
supplied by the caller. It rejects only when **all** hold:

- the repo is not in the known-payer roster
- `bounty_label_of(cand.labels)` is not None
- `open_bounties_here >= config.UNPAID_REPO_THRESHOLD` (10)
- `closed_count == 0`

Reason string: `no payout history (N open bounties here, 0 ever closed)`.

`ever_closed_with` costs one search per repo per label. It is gated behind
`store.reserve_or_wait(conn, "search", 1)` and cached for 24h in `http_cache` under
key `closed-bounty:<repo>:<label>`, with the body holding `{"at": <epoch>, "closed": N}`
so age is checked in Python — no schema change. Only repos already over the open-count
threshold are ever queried, so this is a handful of searches per run.

Applied to the live data: bounty-plaza has 526 open and 0 closed, so it rejects.
A genuine new project that has closed even one bounty does not.

### A2. Quality signals downrank, never reject

`sniper/score.py` gains a `farm_penalty` factor multiplied into the existing
`repo_health`. It uses only fields already present in the repo-detail payload, so it
costs nothing. `Candidate.with_repo_detail` is extended to capture two fields it
currently discards from that same response: `created_at` and `open_issues`.

Three signals, each measured against bounty-plaza:

| signal            | threshold        | bounty-plaza |
| ----------------- | ---------------- | ------------ |
| repo is young     | < 90 days old    | 32 days      |
| few stars         | < 20             | 5            |
| huge open backlog | > 200 open issues| 526          |

All three → 0.25. Exactly two → 0.6. Otherwise 1.0.

This is a heuristic and is documented as one. It exists so a repo that smells like a
farm but has closed a bounty (so A1 abstains) sinks rather than vanishes — the operator
still sees it and can judge.

## B — Linked PR: reject only on a real claim

The current rule treats any open cross-referencing PR as "taken". That is wrong on
every platform now in play:

- **Expensify** selects by *proposal*. A C+ picks one, and only then is that person
  assigned and writes the PR. Bot-raised PRs on Help Wanted issues are noise.
- **Algora** allows multiple attempts. An `/attempt` is not an award and an open PR is
  not an award; only a merge is. A later entrant may read the existing PRs, find their
  flaws, and propose better.

So the only open PR that means "taken" is one whose author is already assigned to the
issue.

`github_api.has_open_linked_pr` becomes:

```python
@dataclass(frozen=True)
class LinkedPRs:
    open_count: int
    claimant: str | None   # login of an open-PR author who is also an assignee

def linked_pr_state(repo: str, number: int, assignees: frozenset[str]) -> LinkedPRs:
```

Same single timeline call, richer answer. The timeline's `cross-referenced` events
already carry `source.issue.state` and `source.issue.user.login`, so no extra request
is needed to learn the author.

`hunt.py` stage 3 then:

- rejects with `claimed by assignee @<login>` when `claimant` is set
- otherwise keeps the candidate, stores `open_pr_count`, and scores it

Scoring: `open_count` is competition, so it joins the existing competition term —
`amount / (1 + comments + 2 * open_count)`. An existing PR costs more than a comment
and less than a wall.

`Candidate` gains two fields here, because `linked_pr_state` needs the assignee logins
to recognise a claimant: `assignees: tuple[str, ...]` populated in `from_issue`, with
the existing `has_assignee` kept as a property derived from it so no caller changes;
and `open_pr_count: int | None` (None = never checked). The `candidates`
table gains an `open_pr_count INTEGER` column, which needs the first real migration in
this project: a `_migrate(conn)` that reads `PRAGMA table_info(candidates)` and issues
`ALTER TABLE ... ADD COLUMN` for anything missing. `CREATE TABLE IF NOT EXISTS` alone
silently leaves an existing database on the old shape.

The 562 rows already rejected must be re-judged, exactly as
`store.apply_payout_rejections` re-judges after a payer-rule change. New
`store.clear_rejections(conn, reason)` sets `rejected = NULL` for rows whose reason
equals `open linked PR`, so the next sweep re-evaluates them under the new rule. A
filter change that only applies to future rows hides its own effect.

## C — Expensify: assignment carries no information

`filters.free_reject` gains a source-aware assignee rule. For `source == "expensify"`,
an assignee is not evidence of anything: Expensify assigns an internal engineer at
posting time, and a C+ when one volunteers. For every other source the existing
behaviour is unchanged — an assignee there is still a claim.

The "has this Expensify job actually been awarded" question is then answered by rule B,
which is the correct test on both platforms: once a C+ picks a contributor, that
contributor is assigned *and* opens the PR, so assignee-plus-their-own-open-PR fires.
One rule, both platforms, no Expensify special case in stage 3.

This step needs no new model fields: `assignees` arrives with B, which requires it
first.

## D — bounties.md: a private gist, linked from Discord on change

New module `sniper/board.py`, one job: render the database as markdown. Pure function
over rows, so it is testable without network or filesystem.

```python
def render(rankable, unrankable, rejected_counts, *, now, warnings) -> str
```

Sections:

1. **Rankable** — has a readable amount, best score first, each row showing amount,
   repo#number, title, comments, age, language, and a `⚠ N PRs` marker when
   `open_pr_count > 0`.
2. **Unrankable** — the ~529 with no readable amount, grouped by repo, newest first.
   This section is the point of the whole feature: it is currently unreachable.
3. **Rejected this sweep** — grouped by reason, count per reason, with up to three
   example issues each. This is how the next "open linked PR = 562" gets caught in
   days rather than months.
4. Footer — sweep timestamp, totals, and any warnings from the run.

Delivery:

- `hunt.py` writes `bounties.md` beside the database every run.
- It then PATCHes a single private gist whose id lives in `.env` as `BOUNTY_GIST_ID`.
  `hunt.py --init-gist` creates it once and prints the id to paste in. If the id is
  unset, the local file is still written and a warning is emitted — the same rule the
  digest already follows for an unset webhook: never fail silently.
- The gist push goes through a small seam (`board.publish(text, *, post=None)`)
  mirroring `notify.send`, so tests inject a fake and no test touches the network.
- Discord gets the gist link only when the unrankable set changes materially: a repo
  appears in it that was not there last sweep, or the count moves by more than 5%. The
  previous sweep's signature is stored in `http_cache` under `board-signature`.
  Otherwise the digest stays as silent as it is today — 144 links a day would train the
  operator to ignore the channel, which is the failure mode the digest already guards.

## Testing

Existing rules hold: 212 tests, none touching the network, fixtures recorded under
`tools/record_fixtures.py`.

- `bounty_label_of`, `farm_penalty`, `render`, and the new scoring term are pure —
  table-driven tests including the measured bounty-plaza numbers as a named case.
- `ever_closed_with` and `linked_pr_state` get recorded fixtures: a farm with zero
  closed, a payer with some, a throttled response (must return None and abstain), a
  timeline with an open PR by a stranger, and one with an open PR by an assignee.
- The migration gets a test that opens a database created from the OLD schema string
  and asserts the new column appears and existing rows survive.
- `clear_rejections` gets a test asserting only the named reason is cleared.

## Order of work

0. Commit the in-flight `verify.py` / `repro.py` / `mailbox.py` / `groundtruth.py`
   changes untouched, so this work starts from a clean tree.
1. **B** — linked PR. 562 rows re-judged; largest single unlock.
2. **C** — Expensify assignee. Opens the $250 channel, which is currently at zero.
3. **A** — farm rejection and downrank. Stops the digest being misinformation.
4. **D** — the board and its gist.

Each step ships independently and is verifiable against the live database on OCI.
