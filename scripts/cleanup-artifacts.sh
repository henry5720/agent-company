#!/usr/bin/env bash
# 每小時的 artifact cleanup（bot container 內做，不從 host 直接刪）：
#
# 1. drafts 內超過 24 小時的 artifact。`slack-thread-artifact` 上傳失敗時刻意留檔（留著
#    才有機會被觀察與人工處理），這支排程是唯一會把那些檔案收掉的東西。刪除邏輯不在
#    這裡：container 內的 `slack-list cleanup` 用 dirfd 逐段走 drafts、不追 symlink；
#    規則正本在 agent-config，這裡不重寫。
# 2. CODEX_HOME `sessions/` 底下 mtime 超過 7 天的 rollout（spec #21 §8）。`log/` 與 state
#    sqlite 不動：find 只從 sessions/ 往下找、只收 `rollout-*.jsonl`。OpenAB session TTL
#    是 4 小時，7 天前的 rollout 不會再被 session/load。
# 3. 量 CODEX_HOME 大小寫給 scripts/alerts.py（> 2 GB 告警）。named volume 在 host 上
#    只有 root 讀得到，所以在 container 內量。
#
# 刻意不從 host 直接刪 runtime/drafts —— 那會繞過唯一的清理規則，也會踩到 SELinux label。
# 一步失敗不跳過後面的步驟；有任何一步失敗就記 FAILED 並回非零，讓 cron 也看得到。
#
# 由 scripts/install-sync-cron.sh 寫進 crontab（每小時 :17，見 lib.sh render_crontab）。
# 手動跑會直接把輸出印在終端機上，cron 跑則寫進 $XDG_STATE_HOME/work-agent/artifact-cleanup.log。
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT"

# Container 內的路徑，正本是 agent-config 的 skills/slack-list/scripts/。這裡不重寫清理規則。
CLEANUP_COMMAND=/home/node/code/agent-config/skills/slack-list/scripts/slack-list
CODEX_HOME_IN_BOT=/home/node/.codex
# 7 天，用分鐘算：find -mtime +7 會進位成「滿 8 天」。
ROLLOUT_MAX_AGE_MINUTES=$((7 * 24 * 60))

cron_job_begin artifact-cleanup || exit 0

bot() { docker compose -f "$ROOT/compose.yaml" exec -T backlog-agent "$@"; }

status=0
if ! bot "$CLEANUP_COMMAND" cleanup; then
  printf 'FAILED: %s cleanup; drafts were not cleaned this run.\n' "$CLEANUP_COMMAND"
  status=1
fi

# sessions/ 不存在（新的 volume）不算失敗。find -delete 不跟 symlink。
# shellcheck disable=SC2016 # $1／$2 由 container 內的 sh 展開
if ! bot sh -c 'test ! -d "$1" || exec find "$1" -xdev -type f -name "rollout-*.jsonl" -mmin "+$2" -print -delete' \
  sh "$CODEX_HOME_IN_BOT/sessions" "$ROLLOUT_MAX_AGE_MINUTES"; then
  printf 'FAILED: CODEX_HOME sessions cleanup; old rollouts were not removed this run.\n'
  status=1
fi

if size=$(bot du -sxb "$CODEX_HOME_IN_BOT" | cut -f1); then
  report_size codex-home-bytes "$size" || status=1
else
  printf 'FAILED: cannot measure CODEX_HOME; the size alert will report it as stale.\n'
  status=1
fi
exit "$status"
