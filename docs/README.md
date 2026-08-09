# docs/

Hard-won context for the bounty tracker. Written 2026-08-09.

Start with [`../HANDOFF.md`](../HANDOFF.md) — it carries the OPEN ITEMS and the system
overview. These files hold the detail behind it.

| file | what it answers |
|---|---|
| [bounty-market-findings.md](bounty-market-findings.md) | Why the digest is four items long and tuning the filters will not fix it |
| [expensify-proposal-path.md](expensify-proposal-path.md) | Why `has_open_linked_pr()` is wrong for Expensify — an open bug |
| [github-api-traps.md](github-api-traps.md) | Six API behaviours that each silently produced "no bounties" |
| [tracker-operations.md](tracker-operations.md) | What runs where, how to check it is alive, how to intervene |

Every number in these files was measured against the live API, not estimated. Re-verify
before acting on any of them months from now.
