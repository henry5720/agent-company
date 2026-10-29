#!/usr/bin/env bash
# 每小時清 thread workspace 與 sandbox 產物（spec #21 §5；#24 決定 3）。各由擁有目錄的
# 一方在 container 內刪，不從 host 直接刪：
#
# 1. thread workspace：bot 內 `thread-workspace cleanup`（7 天沒動刪；總量 > 20 GB 從最久
#    沒動的刪起；被刪的 thread 收到通知）。它最後一行印 `remaining_bytes=<N>`，寫給
#    scripts/alerts.py 判斷「清完仍 > 20 GB」。
# 2. out：sandbox 內 `sandbox-runner cleanup-out`（24 小時）。out 對 bot 唯讀，bot 刪不掉。
#    兩格掛同一個 runtime/out，任一格跑一次就夠；第 1 格 exec 失敗才換第 2 格。
#
# 一步失敗不跳過另一步；任何失敗記 FAILED、回非零。由 install-sync-cron.sh 寫進 crontab
# （每小時 :27）；cron 跑時輸出寫進 $XDG_STATE_HOME/work-agent/workspace-cleanup.log。
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT"

cron_job_begin workspace-cleanup || exit 0

in_service() { docker compose -f "$ROOT/compose.yaml" exec -T "$@"; }

status=0
tw_status=0
output=$(in_service backlog-agent thread-workspace cleanup) || tw_status=$?
printf '%s\n' "$output"
if ((tw_status != 0)); then
  # exit 3 也可能只是通知沒送出（工作副本已經刪了）；細節在上面的 stderr。
  printf 'FAILED: thread-workspace cleanup exited %d.\n' "$tw_status"
  status=1
fi
remaining=$(printf '%s\n' "$output" | sed -n 's/^remaining_bytes=\([0-9][0-9]*\)$/\1/p' | tail -n1)
if [[ -n $remaining ]]; then
  report_size thread-workspace-bytes "$remaining" || status=1
else
  printf 'FAILED: thread-workspace cleanup reported no remaining_bytes; the size alert will report it as stale.\n'
  status=1
fi

if ! in_service prototype-sandbox-1 sandbox-runner cleanup-out; then
  printf 'sandbox 1 cleanup-out failed; trying sandbox 2.\n'
  if ! in_service prototype-sandbox-2 sandbox-runner cleanup-out; then
    printf 'FAILED: sandbox-runner cleanup-out in both sandboxes; out was not cleaned this run.\n'
    status=1
  fi
fi
exit "$status"
