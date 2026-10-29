#!/usr/bin/env bash
# 刪掉 drafts 內超過 24 小時的 artifact。
#
# `slack-thread-artifact` 上傳失敗時刻意留檔（留著才有機會被觀察與人工處理），
# 這支排程是唯一會把那些檔案收掉的東西。沒有它，「最多保留 24 小時」只是文件。
#
# 刪除邏輯本身不在這裡：container 內的 `slack-list cleanup` 用 dirfd 逐段走
# drafts、不追 symlink。這支只負責排程、呼叫與記錄。drafts 在 home volume 裡，
# host 本來就碰不到；也不要繞過那份唯一的清理規則。
#
# 由 scripts/install-sync-cron.sh 寫進 crontab（每小時 :17）。手動跑會直接把輸出
# 印在終端機上，cron 跑則寫進 $XDG_STATE_HOME/agent-company/artifact-cleanup.log。
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT"

STATE_DIR=${XDG_STATE_HOME:-${HOME:?HOME must be set}/.local/state}/agent-company
LOG=${CLEANUP_LOG:-$STATE_DIR/artifact-cleanup.log}
LOG_MAX_BYTES=${CLEANUP_LOG_MAX_BYTES:-1048576}
LOCK=${CLEANUP_LOCK:-$STATE_DIR/artifact-cleanup.lock}
# Container 內的路徑，正本是 agent-config 的 skills/slack-list/scripts/。這裡不重寫清理規則。
CLEANUP_COMMAND=/home/node/snapshots/agent-config/skills/slack-list/scripts/slack-list

mkdir -p "$STATE_DIR"

# cron has no journald: keep one rotation of our own log, but stay on the
# terminal when a maintainer runs this by hand.
if [[ ! -t 1 ]]; then
  if [[ -f "$LOG" && $(stat -c '%s' "$LOG") -gt $LOG_MAX_BYTES ]]; then
    mv -f "$LOG" "$LOG.1"
  fi
  exec >>"$LOG" 2>&1
fi
printf '=== %s\n' "$(date -Is)"

exec 9>"$LOCK"
if ! flock -n 9; then
  printf 'another cleanup run is still holding %s; skipping this hour.\n' "$LOCK"
  exit 0
fi

status=0
docker compose -f "$ROOT/compose.yaml" exec -T agent "$CLEANUP_COMMAND" cleanup || status=$?
if ((status != 0)); then
  # 記下來並回非零，讓 cron 也看得到。清不掉時 artifact 會繼續累積，這不是可以
  # 安靜吞掉的事。
  printf 'FAILED: %s cleanup exited %d; drafts were not cleaned this run.\n' \
    "$CLEANUP_COMMAND" "$status" >&2
  exit "$status"
fi
