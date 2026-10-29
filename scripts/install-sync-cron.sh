#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT"

SNAPSHOT_ROOT=${SNAPSHOT_ROOT:-$HOME/work-agent-snapshots}
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/work-agent
SCHEDULE=${SNAPSHOT_SCHEDULE:-0 * * * *}
CLEANUP_SCHEDULE=${CLEANUP_SCHEDULE:-17 * * * *}

# Create .index before any docker run, or Docker creates the bind source as root
# and a host user without sudo can no longer write the CodeGraph index.
mkdir -p "$SNAPSHOT_ROOT/.index" "$STATE_DIR" "$ROOT/runtime/openab" "$ROOT/runtime/drafts"
chmod 0750 "$ROOT/runtime/openab" "$ROOT/runtime/drafts"

# Rewrite our own lines, leave every other crontab entry untouched. All jobs are
# installed together on purpose: a host that syncs snapshots but never runs the
# cleanups keeps failed uploads, thread workspaces and rollouts forever, and one
# without the alert job finds out when the disk is already full.
marker_args=()
for marker in "${CRON_MARKERS[@]}"; do marker_args+=(-e "$marker"); done
{
  crontab -l 2>/dev/null | grep -vF "${marker_args[@]}" || true
  render_crontab "$ROOT" "$SNAPSHOT_ROOT" "$SCHEDULE" "$CLEANUP_SCHEDULE"
} | crontab -

printf 'Installed crontab entries:\n'
crontab -l | grep -F "${marker_args[@]}"

printf '\nRunning the first sync (this clones every repo in config/repos.conf)...\n'
SNAPSHOT_ROOT="$SNAPSHOT_ROOT" "$ROOT/scripts/update-snapshots.sh"
