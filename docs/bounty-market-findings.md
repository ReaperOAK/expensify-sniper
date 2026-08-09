# What the GitHub bounty market actually looks like

Measured 2026-08-09 against the live GitHub and Algora APIs, from a full 1,506-candidate
sweep. **Do not tune the tracker's filters expecting a longer list — the filters are not
what is limiting it.**

## Visible inventory is inversely correlated with inventory that pays

| repo | open bounties | ever rewarded | ever closed |
|---|---:|---:|---:|
| ClankerNation/OpenAgents | 201 | **0** | **0** |
| UnsafeLabs/Bounty-Hunters | 182 | **0** | **0** |
| activepieces/activepieces | 1 | **150** | 173 |

The first two advertised **$917,000** between them and took the entire top of the digest on
amount alone. Neither has ever paid anyone, or even closed a bounty issue. The third
offered $200 and has paid out 150 times.

Expected value ranks them exactly backwards unless payout history is part of it. That is
why `sniper/payers.py` exists.

## The real payers hold almost no standing inventory

```
Algora issues ever rewarded                          3,114
  ...updated in the last 90 days                       501
  ...updated in the last 30 days                        10

open UNASSIGNED bounties at the 10 most-recent payers     3
  Mudlet 0 · qdrant 0 · httpx 0 · Cap 0 · documenso 0
  outerbase 0 · tscircuit 0 · gitea 2 · highlight 1
```

Expensify is the same shape: **86 open Help Wanted, 0 unassigned, 6,264 closed all time.**

Anything real is claimed within hours. What a periodic search still finds is, by
construction, what nobody wanted. This is why the 10-minute payer watch exists and why the
3-hourly digest cannot win a real bounty on its own.

## Absence of an Algora label is not evidence of non-payment

`commaai/opendbc` runs its own programme and carries no Algora label, so it shows 0
rewarded — while having closed **7** bounties including a $4,000 Rivian Gen2 port
(2026-04-17) and a $250 one (2026-03-14). `payers.payout_reject()` is deliberately narrow
for this reason: it only fires when a repo is wearing Algora's own label, has never been
rewarded, and is advertising ≥10 open bounties.

## Two traps in individual issues

- **`commaai/opendbc#3426`** advertised $10,000, was opened by comma's engineering lead,
  and passed every automated check. Its eligibility link 301s to a Rick Astley video. Only
  a human following the link could know. Dismissed via `hunt.py --dismiss`.
- **`go-gitea/gitea#1872`** carries `$250`, `$50` and `$500`. On a farm that means spam; on
  a proven payer it means **tiers**. Payout history decides, and the candidate is priced at
  its lowest tier — the only figure the operator is actually promised.

## If it stays quiet

Widen rather than loosen. The payer roster is capped at the 43 most-recent payers and there
are ~3,100 rewarded issues of history to mine. Dropping the $50 floor is also reasonable: a
real $50 from a proven payer beats a fake $9,000.
