#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT"

SNAPSHOT_ROOT=${SNAPSHOT_ROOT:-$HOME/agent-company-snapshots}
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/agent-company
SCHEDULE=${SNAPSHOT_SCHEDULE:-0 * * * *}
CLEANUP_SCHEDULE=${CLEANUP_SCHEDULE:-17 * * * *}

# Create .index before any docker run, or Docker creates the bind source as root
# and a host user without sudo can no longer write the CodeGraph index.
mkdir -p "$SNAPSHOT_ROOT/.index" "$STATE_DIR"

# Rewrite our own lines, leave every other crontab entry untouched. Both jobs are
# installed together on purpose: a host that syncs snapshots but never runs the
# artifact cleanup keeps failed uploads in drafts forever.
{
  # 改名前的 `# work-agent-*` 兩行也一起拿掉，不然新舊排程會同時跑。
  crontab -l 2>/dev/null | grep -vF -e "$CRON_MARKER_SYNC" -e "$CRON_MARKER_CLEANUP" \
    -e '# work-agent-snapshots' -e '# work-agent-artifact-cleanup' || true
  render_crontab "$ROOT" "$SNAPSHOT_ROOT" "$SCHEDULE" "$CLEANUP_SCHEDULE"
} | crontab -

printf 'Installed crontab entries:\n'
crontab -l | grep -F -e "$CRON_MARKER_SYNC" -e "$CRON_MARKER_CLEANUP"

printf '\nRunning the first sync (this clones every repo in config/repos.conf)...\n'
SNAPSHOT_ROOT="$SNAPSHOT_ROOT" "$ROOT/scripts/update-snapshots.sh"
