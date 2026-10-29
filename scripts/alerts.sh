#!/usr/bin/env bash
# 每小時的 host 告警（spec #21 §8）。判斷邏輯在 scripts/alerts.py；這支只負責排程、
# log 與鎖。排程時間的正本是 scripts/lib.sh 的 `render_crontab`（排在兩個清理之後，讀到的
# 是這一小時剛回報的大小）。cron 跑時輸出寫進 $XDG_STATE_HOME/work-agent/alerts.log。
#
# 需要 deployment host 上兩樣東西（docs/runbook.md「告警」）：
# - webhook URL 檔：預設 ~/.config/work-agent/alert-webhook（0600），可用 WORK_AGENT_ALERT_WEBHOOK_FILE 改。
# - 讀得到 host proxy 的 access log（/var/log/work-agent-proxy/）：cron 使用者要在
#   work-agent-proxy group 裡，由 install-host-network.sh 加。
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT"

cron_job_begin alerts || exit 0

args=(--state-dir "$STATE_DIR")
[[ -n ${WORK_AGENT_ALERT_WEBHOOK_FILE:-} ]] && args+=(--webhook-file "$WORK_AGENT_ALERT_WEBHOOK_FILE")
[[ -n ${WORK_AGENT_PROXY_LOG_DIR:-} ]] && args+=(--log-dir "$WORK_AGENT_PROXY_LOG_DIR")
exec python3 "$ROOT/scripts/alerts.py" "${args[@]}"
