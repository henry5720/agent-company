#!/usr/bin/env bash
# 關掉 chrome sidecar default context 裡連續 4 小時沒換過網址的分頁。
#
# 瀏覽器回收的主力不在這裡：thread 的 isolated context 由 playwright-cli 自己收（`close`，
# 或閒置 `PLAYWRIGHT_MCP_IDLE_TIMEOUT` 後 detach，見 config/openab.toml）。這支只兜底
# default context，那裡的分頁沒有任何 session 會回來收。判斷規則在 reap-browser-tabs.mjs，
# 理由見 docs/adr/0016-reclaim-browser-tabs-by-idleness.md。
#
# 由 scripts/install-sync-cron.sh 寫進 crontab（每小時 :37）。手動跑會直接把輸出印在終端機上，
# cron 跑則寫進 $XDG_STATE_HOME/agent-company/browser-reap.log。`REAP_DRY_RUN=1` 只列出會關哪些。
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT"

STATE_DIR=${XDG_STATE_HOME:-${HOME:?HOME must be set}/.local/state}/agent-company
LOG=${REAP_LOG:-$STATE_DIR/browser-reap.log}
LOG_MAX_BYTES=${REAP_LOG_MAX_BYTES:-1048576}
LOCK=${REAP_LOCK:-$STATE_DIR/browser-reap.lock}

mkdir -p "$STATE_DIR"

if [[ ! -t 1 ]]; then
  if [[ -f "$LOG" && $(stat -c '%s' "$LOG") -gt $LOG_MAX_BYTES ]]; then
    mv -f "$LOG" "$LOG.1"
  fi
  exec >>"$LOG" 2>&1
fi
printf '=== %s\n' "$(date -Is)"

exec 9>"$LOCK"
if ! flock -n 9; then
  printf 'another reap run is still holding %s; skipping this hour.\n' "$LOCK"
  exit 0
fi

status=0
docker compose -f "$ROOT/compose.yaml" exec -T -e REAP_DRY_RUN="${REAP_DRY_RUN:-0}" agent \
  node --input-type=module <"$ROOT/scripts/reap-browser-tabs.mjs" || status=$?
if ((status != 0)); then
  printf 'FAILED: browser tab reap exited %d; default-context tabs were not reclaimed this run.\n' \
    "$status" >&2
  exit "$status"
fi
