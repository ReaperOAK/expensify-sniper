#!/usr/bin/env bash
# Install the bounty tracker on the OCI box and schedule it every 3 hours.
#
#   ./deploy/install-hunt-cron.sh --check     # dry run: verify, change nothing
#   ./deploy/install-hunt-cron.sh --show      # print the remote crontab, exit
#   ./deploy/install-hunt-cron.sh             # sync + smoke test + install
#
# Idempotent: the existing hunt.py crontab line is removed before the new one is
# added, so re-running never stacks duplicates.
set -euo pipefail

HOST="${OCI_HOST:-openclaw}"
# Home-relative on purpose. rsync resolves a remote path against the login
# home and does NOT run a shell over it, so a literal "$HOME/..." here arrives
# unexpanded and rsync tries to create a directory called '$HOME'.
REMOTE="${OCI_PATH:-expensify-sniper}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# NEVER ship these. .env holds live credentials and the box has its own copy;
# .git is a worktree pointer file here and would be meaningless (and broken)
# remotely; sniper.sqlite is local run state and would clobber the box's.
EXCLUDES=(
    --exclude '.git'          --exclude '.git/'
    --exclude '.env'          --exclude '.venv/'
    --exclude '.auth-state.json'
    --exclude '.claude-clean/'
    --exclude '.superpowers/' --exclude '.worktrees/'
    --exclude '__pycache__/'  --exclude '*.pyc'
    --exclude '*.sqlite'      --exclude '*.sqlite-journal'
    --exclude '*.log'         --exclude '*.jsonl'
    --exclude 'corpus/'       --exclude 'runs/'  --exclude 'evidence/'
    --exclude 'verify-runs/'  --exclude 'bt/'    --exclude '.impeccable/'
)

case "${1:-}" in
--show)
    ssh "$HOST" 'crontab -l 2>/dev/null || echo "(no crontab)"'
    exit 0
    ;;
esac

DRY=""
[[ "${1:-}" == "--check" ]] && DRY="--dry-run"

echo "==> preflight: the payload must contain no credential"
if rsync -an --out-format='%n' "${EXCLUDES[@]}" "$HERE/" /tmp/_deploy_preflight/ \
     | grep -E '(^|/)\.env$|auth-state|credentials|\.claude-clean' ; then
    echo "REFUSING: a credential path is in the rsync payload" >&2
    exit 1
fi
echo "    clean"

echo "==> preflight: remote python and requests"
ssh "$HOST" '/usr/bin/python3 -c "import requests, sqlite3; print(\"  python3 ok, requests\", requests.__version__)"'

echo "==> preflight: remote .env exists and is 0600"
ssh "$HOST" "test -f $REMOTE/.env || { echo '  MISSING $REMOTE/.env' >&2; exit 1; }
             perms=\$(stat -c %a $REMOTE/.env)
             [ \"\$perms\" = 600 ] || { echo \"  .env is \$perms, expected 600\" >&2; exit 1; }
             echo '  .env present, 0600'"

echo "==> syncing source${DRY:+ (DRY RUN)}"
rsync -az $DRY --delete "${EXCLUDES[@]}" "$HERE/" "$HOST:$REMOTE/"

if [[ -n "$DRY" ]]; then
    echo "==> --check: stopping before smoke test and crontab install"
    exit 0
fi

echo "==> smoke test: wiring only, deliberately NOT a full run"
# A full backlog run takes tens of minutes (paced pages, waited-out throttles)
# and would spend the same token the cron is about to use. This proves the
# modules import, config loads, the DB opens and the CLI is wired -- everything
# a deploy can break. Whether GitHub is cooperating is the cron's business, and
# it reports that itself, in the digest, by design.
ssh "$HOST" "cd $REMOTE && /usr/bin/python3 - <<'PY'
import hunt
from sniper import config, store
conn = store.connect()
print('  imports ok; sources:', ','.join(config.HUNT_ENABLED_SOURCES))
print('  db ok; candidates already stored:', store.candidate_count(conn))
print('  delivery:', 'discord' if config.DISCORD_WEBHOOK
      else ('telegram' if config.TELEGRAM_BOT_TOKEN else 'NONE CONFIGURED'))
# The board is a second delivery path and fails the same way: publish() returns
# None for an unset id rather than raising, so a box deployed without it pushes
# a digest that never carries a board link and never says why. Unset is the
# state a fresh box is IN -- nothing here writes BOUNTY_GIST_ID -- so the deploy
# has to say so out loud, exactly as it does for the webhook above.
print('  board gist:', config.BOUNTY_GIST_ID
      or 'NOT CONFIGURED (run: python3 hunt.py --init-gist, then add it to .env)')
hunt.build_sources(config.HUNT_ENABLED_SOURCES)
print('  sources build ok')
PY"

echo "==> preflight: flock, which both cron jobs depend on"
# Both jobs share one lock and one database. Installing a crontab that calls a
# missing flock would make EVERY sweep fail with 127 into a log nobody reads --
# a tracker that reports nothing looks exactly like a market with nothing in it,
# which is the failure this whole project is built against.
ssh "$HOST" "test -x /usr/bin/flock || { echo '  MISSING /usr/bin/flock -- install util-linux'; exit 1; }
             echo '  flock ok'"

echo "==> installing crontab entry"
# One lock PER JOB, so a job cannot overlap itself and the digest cannot block
# the watch. A shared lock starved 2-4 watch cycles per digest, which is exactly
# backwards: the watch exists to catch a label before it rolls off a page that
# covered 27 minutes when measured, so starving it loses labels permanently.
# -n rather than -w: a sweep that cannot get its lock skips, since queueing
# would build a backlog racing to report the same inventory.
ssh "$HOST" "crontab -l 2>/dev/null | grep -v 'hunt.py' > /tmp/ct.\$\$ || true
             cat >> /tmp/ct.\$\$ <<'CRON'
PATH=/usr/local/bin:/usr/bin:/bin:\$HOME/.local/bin
DIGEST_LOCK=/tmp/expensify-sniper-digest.lock
WATCH_LOCK=/tmp/expensify-sniper-watch.lock
0 */3 * * * cd \$HOME/expensify-sniper && /usr/bin/flock -n \$DIGEST_LOCK /usr/bin/python3 hunt.py >> \$HOME/expensify-sniper/hunt.log 2>&1
*/10 * * * * cd \$HOME/expensify-sniper && /usr/bin/flock -n \$WATCH_LOCK /usr/bin/python3 hunt.py --watch >> \$HOME/expensify-sniper/watch.log 2>&1
CRON
             crontab /tmp/ct.\$\$ && rm -f /tmp/ct.\$\$"

echo "==> installed:"
ssh "$HOST" "crontab -l | grep hunt.py"
