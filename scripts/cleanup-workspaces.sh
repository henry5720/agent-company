#!/usr/bin/env bash
# 每小時清 thread workspace 與 sandbox 產物（spec #21 §5；#24 決定 3）。各由擁有目錄的
# 一方在 container 內刪，不從 host 直接刪：
#
# 1. thread workspace：bot 內 `thread-workspace cleanup`。保留天數與總量上限的正本是
#    agents/bin/thread-workspace（MAX_AGE_SECONDS、MAX_TOTAL_BYTES）；被刪的 thread 收到
#    通知。它最後一行印 `remaining_bytes=<N>`，寫給 scripts/alerts.py 判斷「清完仍超過上限」。
# 2. out：sandbox 內 `sandbox-runner cleanup-out`（保留時間的正本是 sandbox/runner 的
#    OUT_MAX_AGE_SECONDS）。out 對 bot 唯讀，bot 刪不掉。兩格掛同一個 runtime/out，
#    任一格跑一次就夠；第 1 格 exec 不進去才換第 2 格（lib.sh `live_sandbox`），
#    指令本身失敗不換格重跑。
#
# 一步失敗不跳過另一步；任何失敗記 FAILED、回非零。排程時間的正本是 scripts/lib.sh 的
# `render_crontab`；cron 跑時輸出寫進 $XDG_STATE_HOME/work-agent/workspace-cleanup.log。
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

if ! sandbox=$(live_sandbox "$ROOT"); then
  printf 'FAILED: cannot exec into either sandbox; out was not cleaned this run.\n'
  status=1
elif ! in_service "$sandbox" sandbox-runner cleanup-out; then
  printf 'FAILED: sandbox-runner cleanup-out in %s; out was not cleaned this run.\n' "$sandbox"
  status=1
fi
exit "$status"
