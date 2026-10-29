#!/usr/bin/env bash
# 每週一次 `pnpm store prune`（spec #21 §8）：兩格 sandbox 共用的 pnpm-store volume 只會長
# 不會縮。在 sandbox 內跑（store 是它的 volume；bot 不碰 repo 的 install）。
#
# pnpm 不同 major 用不同的 store 子目錄（v3、v10…），用錯版本 prune 等於沒 prune。
# sandbox 經 corepack 依各 repo 的 packageManager 取 pnpm，所以這裡從 snapshot root 的
# 各 repo package.json 收集實際用到的 pnpm 版本，每個版本各 prune 一次。
#
# prune 只刪沒被任何 project 引用的 package；排在週日清晨，避開 PM 正在 install 的時段。
# 由 install-sync-cron.sh 寫進 crontab（週日 04:37）；輸出寫進
# $XDG_STATE_HOME/work-agent/pnpm-prune.log。
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT"
SNAPSHOT_ROOT=${SNAPSHOT_ROOT:?set SNAPSHOT_ROOT in .env}

cron_job_begin pnpm-prune || exit 0

mapfile -t versions < <(python3 - "$SNAPSHOT_ROOT" <<'PY'
import json, pathlib, re, sys
seen = set()
for manifest in sorted(pathlib.Path(sys.argv[1]).glob("*/package.json")):
    try:
        pm = json.loads(manifest.read_text()).get("packageManager", "")
    except (OSError, ValueError):
        continue
    match = re.fullmatch(r"pnpm@(\d+\.\d+\.\d+)(\+.*)?", pm if isinstance(pm, str) else "")
    if match and match.group(1) not in seen:
        seen.add(match.group(1))
        print(match.group(1))
PY
)
if ((${#versions[@]} == 0)); then
  printf 'no snapshot repo pins pnpm; nothing to prune.\n'
  exit 0
fi

in_sandbox() {
  docker compose -f "$ROOT/compose.yaml" exec -T -w /home/node prototype-sandbox-1 "$@" ||
    docker compose -f "$ROOT/compose.yaml" exec -T -w /home/node prototype-sandbox-2 "$@"
}

status=0
for version in "${versions[@]}"; do
  printf -- '--- pnpm@%s store prune\n' "$version"
  if ! in_sandbox corepack "pnpm@$version" store prune; then
    printf 'FAILED: pnpm@%s store prune.\n' "$version"
    status=1
  fi
done
exit "$status"
