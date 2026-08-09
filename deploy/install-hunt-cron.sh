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
REMOTE="${OCI_PATH:-\$HOME/expensify-sniper}"
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

echo "==> smoke test (dry run: nothing is sent)"
ssh "$HOST" "cd $REMOTE && timeout 1800 /usr/bin/python3 hunt.py --dry-run 2>&1 | head -12"

echo "==> installing crontab entry"
ssh "$HOST" "crontab -l 2>/dev/null | grep -v 'hunt.py' > /tmp/ct.\$\$ || true
             cat >> /tmp/ct.\$\$ <<'CRON'
PATH=/usr/local/bin:/usr/bin:/bin:\$HOME/.local/bin
0 */3 * * * cd \$HOME/expensify-sniper && /usr/bin/python3 hunt.py >> \$HOME/expensify-sniper/hunt.log 2>&1
CRON
             crontab /tmp/ct.\$\$ && rm -f /tmp/ct.\$\$"

echo "==> installed:"
ssh "$HOST" "crontab -l | grep hunt.py"
