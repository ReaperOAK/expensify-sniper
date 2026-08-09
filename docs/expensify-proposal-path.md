# Expensify: selection is by proposal, not by PR

**This documents an open bug. Not fixed as of 2026-08-09.**

## The mechanism

Expensify Help Wanted bounties are awarded by **proposal**:

1. an issue gets `Help Wanted`
2. contributors post proposals in the comments
3. a Contributor+ (C+) selects one
4. only then is that person assigned, and only then do they write the PR

Many people now run AI agents that auto-raise PRs on Help Wanted issues. **Those PRs do not
claim anything.** They are noise.

## The bug

`hunt.py` stage 3 applies `github_api.has_open_linked_pr()` to **every** surviving
candidate, regardless of source:

```python
taken = github_api.has_open_linked_pr(cand.repo, cand.number)
if taken:
    store.upsert_candidate(conn, cand, rejected="open linked PR", now=stamp)
```

On Algora repos this is correct — there, the PR *is* the claim. On Expensify it is wrong,
and it silently discards still-winnable $250 bounties as "taken", in the highest-value
channel available.

## The fix, and the judgement it needs

Make the filter source-aware: skip it for `source == "expensify"`, or demote it from a
reject to a displayed signal. The open question is whether bot-raised PRs need
distinguishing from genuine ones (MelvinBot, ProposalPolice, OSBotify and friends are
already enumerated in `config.BOT_AUTHORS`) — and whether the same bot noise is also
over-rejecting on Algora repos, where all three live bounties checked on 2026-08-09 were
rejected for exactly this reason.

Whoever knows the Expensify flow best should make that call.
