# Operating the tracker

## What runs where

Laptop: `~/Documents/expensify-sniper`, branch `bounty-tracker` (`master` holds the clean
pre-work baseline). OCI box: `ssh openclaw`, `~/expensify-sniper`, two crons:

```
0 */3 * * *   hunt.py           # backlog digest: github-labels + expensify
*/10 * * * *  hunt.py --watch   # 43 proven-payer repos + Expensify
```

Deploy with `./deploy/install-hunt-cron.sh` — idempotent, refuses to run if a credential
path is in the rsync payload, and checks the remote `.env` is 0600 before touching
anything. `--check` dry-runs it; `--show` prints the remote crontab.

## Is it alive?

**Judge liveness by `watch.log` growing, never by Discord silence.** Quiet sweeps are
deliberately silent — 144 "nothing new" messages a day would train you to ignore the
channel, so the one message that matters would land where nobody looks. Warnings always
send: "nothing found" and "I could not look" must never be the same silence.

```bash
ssh openclaw 'cd ~/expensify-sniper
echo "sweeps: $(grep -c "" watch.log)"
echo "last:   $(stat -c %y watch.log | cut -d. -f1)"
grep "⚠" watch.log | tail -5'
```

## Intervening

```bash
hunt.py --dry-run                     # run, print, send nothing
hunt.py --watch                       # fast sweep only
hunt.py --dismiss REPO#NUMBER --reason '...'   # kill an issue permanently
hunt.py --list-dismissed
hunt.py --undismiss REPO#NUMBER
```

Dismissals are retroactive: they re-judge rows already stored, not just future discoveries.
So is the payout filter — a filter that ships later must reach what is already in the table,
or the digest keeps recommending what it now knows to be a farm.

## Tuning

Everything is env-overridable; see the bounty-tracker block in `sniper/config.py`.
The ones that matter: `BOUNTY_QUERIES`, `AMOUNT_FLOOR_USD` (50), `DIGEST_MAX_PER_REPO` (3),
`UNPAID_REPO_THRESHOLD` (10), `WATCH_EVERY_MINUTES` (10), `SEARCH_PER_PAGE` (50 — see
[github-api-traps.md](github-api-traps.md), do not raise it).

## Invariants

- `verify.py` is byte-identical to the pre-work baseline and must stay that way.
- The `seen_events`, `bids` and `http_cache` tables are untouched; an old DB gains the new
  tables with no migration.
- No test touches the network. `tests/conftest.py` seeds a dummy `GITHUB_TOKEN` so the real
  credential never enters the pytest process.
