# Shared helpers. Source this, do not execute it.

# Read KEY=value pairs from the repo's .env without executing it, expanding the
# literal ${HOME} that .env.example ships with. Values already present in the
# environment win.
load_env() {
  local root=$1 line key value
  [[ -f "$root/.env" ]] || return 0
  while IFS= read -r line; do
    [[ $line =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    key=${line%%=*}
    value=${line#*=}
    value=${value#\"}; value=${value%\"}
    value=${value#\'}; value=${value%\'}
    value=${value//'${HOME}'/$HOME}
    [[ -n ${!key:-} ]] && continue
    printf -v "$key" '%s' "$value"
    export "${key?}"
  done < "$root/.env"
}

# The deployment host's scheduled jobs. Every marker is a grep target, so a
# reinstall rewrites our own lines and leaves every other crontab entry alone.
CRON_MARKER_SYNC='# work-agent-snapshots'
CRON_MARKER_CLEANUP='# work-agent-artifact-cleanup'
CRON_MARKER_WORKSPACE='# work-agent-workspace-cleanup'
CRON_MARKER_PNPM='# work-agent-pnpm-prune'
CRON_MARKER_ALERTS='# work-agent-alerts'
CRON_MARKERS=("$CRON_MARKER_SYNC" "$CRON_MARKER_CLEANUP" "$CRON_MARKER_WORKSPACE"
  "$CRON_MARKER_PNPM" "$CRON_MARKER_ALERTS")

# Print the crontab lines the host is supposed to run. install-sync-cron.sh pipes
# this into `crontab -`; tests/static.sh and tests/host-cron.py render it to prove
# every job is actually scheduled rather than only described in the docs. Keeping
# one renderer means the tested text is the installed text.
#
# Minutes are spread so nothing lands on the sync run at :00, which rebuilds the
# CodeGraph indexes with a docker run and is the heavy job of the hour:
#   :17 artifact cleanup — drafts（agent-config slack-list）＋ CODEX_HOME sessions
#   :27 thread workspace（7 天／20 GB，bot 內）＋ out（24 小時，sandbox 內）
#   :47 alerts — after both cleanups, so it reads this hour's reported sizes
#   Sunday 04:37 pnpm store prune（sandbox 內）
render_crontab() {
  local root=$1 snapshot_root=$2
  local sync_schedule=${3:-0 * * * *}
  local cleanup_schedule=${4:-17 * * * *}
  printf '%s SNAPSHOT_ROOT=%q %q %s\n' \
    "$sync_schedule" "$snapshot_root" "$root/scripts/update-snapshots.sh" "$CRON_MARKER_SYNC"
  printf '%s %q %s\n' \
    "$cleanup_schedule" "$root/scripts/cleanup-artifacts.sh" "$CRON_MARKER_CLEANUP"
  printf '%s %q %s\n' \
    '27 * * * *' "$root/scripts/cleanup-workspaces.sh" "$CRON_MARKER_WORKSPACE"
  printf '%s SNAPSHOT_ROOT=%q %q %s\n' \
    '37 4 * * 0' "$snapshot_root" "$root/scripts/prune-pnpm-store.sh" "$CRON_MARKER_PNPM"
  printf '%s %q %s\n' \
    '47 * * * *' "$root/scripts/alerts.sh" "$CRON_MARKER_ALERTS"
}

# Shared start of every scheduled job: state dir, one rotation of its own log
# (cron has no journald), and a lock so a slow run is skipped rather than doubled.
# A maintainer running the script by hand keeps the output on the terminal.
#   cron_job_begin <name>   → $STATE_DIR, log $STATE_DIR/<name>.log, lock <name>.lock
# Returns 1 when another run still holds the lock; callers `|| exit 0`.
cron_job_begin() {
  local name=$1 log lock max=${CRON_LOG_MAX_BYTES:-1048576}
  STATE_DIR=${XDG_STATE_HOME:-${HOME:?HOME must be set}/.local/state}/work-agent
  mkdir -p "$STATE_DIR"
  log=$STATE_DIR/$name.log
  lock=$STATE_DIR/$name.lock
  if [[ ! -t 1 ]]; then
    if [[ -f "$log" && $(stat -c '%s' "$log") -gt $max ]]; then
      mv -f "$log" "$log.1"
    fi
    exec >>"$log" 2>&1
  fi
  printf '=== %s\n' "$(date -Is)"
  exec 9>"$lock"
  if ! flock -n 9; then
    printf 'another %s run is still holding %s; skipping this hour.\n' "$name" "$lock"
    return 1
  fi
}

# Record a measured size for scripts/alerts.py: one line "<epoch> <bytes>",
# written to a temp file and renamed so the alert never reads half a line.
report_size() {
  local name=$1 bytes=$2 tmp
  [[ $bytes =~ ^[0-9]+$ ]] || { printf 'FAILED: %s size %q is not a number\n' "$name" "$bytes"; return 1; }
  tmp=$(mktemp "$STATE_DIR/.$name.XXXXXX")
  printf '%s %s\n' "$(date +%s)" "$bytes" >"$tmp"
  mv -f "$tmp" "$STATE_DIR/$name"
}
