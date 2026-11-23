#!/usr/bin/env bash
# 刪掉 drafts 內超過 24 小時的 artifact。
#
# agent 傳回 Discord 的檔案留在 drafts（上傳不刪檔），這支排程是唯一會把它們收掉的東西。
# 沒有它，「最多保留 24 小時」只是文件。
#
# 刪除在 container 內用 GNU find 做：預設 -P 不追 symlink（drafts 自己是 symlink 時
# 也只看那個 symlink、不往下走），-type f 只挑 regular file，-delete 用 unlinkat
# 對著已開的目錄 fd 刪，不重新走 path。drafts 在 home volume 裡，host 本來就碰不到。
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
DRAFTS=/home/node/drafts

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
# drafts 還沒建過（從沒產出 artifact）時沒有東西可清，不算失敗。
docker compose -f "$ROOT/compose.yaml" exec -T agent sh -c \
  'test ! -e "$1" || find "$1" -mindepth 1 -type f -mmin +1440 -print -delete' sh "$DRAFTS" ||
  status=$?
if ((status != 0)); then
  # 記下來並回非零，讓 cron 也看得到。清不掉時 artifact 會繼續累積，這不是可以
  # 安靜吞掉的事。
  printf 'FAILED: %s cleanup exited %d; drafts were not cleaned this run.\n' \
    "find $DRAFTS" "$status" >&2
  exit "$status"
fi
