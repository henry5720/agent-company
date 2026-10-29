#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT"

SNAPSHOT_ROOT=${SNAPSHOT_ROOT:-$HOME/work-agent-snapshots}
HOST_UID=${HOST_UID:-1000}
HOST_GID=${HOST_GID:-1000}
ENV_FILE="$ROOT/env/openab.env"
STATE_DIR="$ROOT/runtime/openab"
DRAFT_DIR="$ROOT/runtime/drafts"
R2_ALLOWED_HOST="99de68928da234ebcf0c9370443ad7ee.r2.cloudflarestorage.com"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

[[ -f "$ENV_FILE" ]] || fail "missing $ENV_FILE (copy env/openab.env.example and fill secrets)"
[[ $(stat -c '%a' "$ENV_FILE") =~ ^(600|640)$ ]] || fail "$ENV_FILE must have mode 600 or 640"

required_keys=(
  SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_LIST_ID SLACK_TEAM_ID SLACK_WORKSPACE_URL WORK_HELPER_ISSUE_MODE
  R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY
)
for key in "${required_keys[@]}"; do
  grep -q "^${key}=." "$ENV_FILE" || fail "$key is missing from $ENV_FILE"
done
grep -q '^WORK_HELPER_ISSUE_MODE=manual$' "$ENV_FILE" || fail "WORK_HELPER_ISSUE_MODE must be manual"
grep -q '^SLACK_BOT_TOKEN=xoxb-' "$ENV_FILE" || fail "SLACK_BOT_TOKEN must start with xoxb-"
grep -q '^SLACK_APP_TOKEN=xapp-' "$ENV_FILE" || fail "SLACK_APP_TOKEN must start with xapp-"
# env_file loads this whole file into the bot container. The gateway key lives only in
# the host proxy (/etc/work-agent/gateway.env); here the agent could read it. The bot's
# COMPANY_GATEWAY_BASE_URL is the proxy address that compose.yaml sets over env_file.
! grep -q '^COMPANY_GATEWAY_API_KEY=' "$ENV_FILE" ||
  fail "remove COMPANY_GATEWAY_API_KEY from $ENV_FILE (the key belongs to the host proxy; see scripts/install-host-network.sh gateway-env)"
! grep -q 'replace-me' "$ENV_FILE" || fail "$ENV_FILE still contains placeholder values"

# R2 credentials are required for both OpenAB configs. Do not probe R2 here:
# presence, placeholder rejection and the exact configured host are the only
# deployment-time checks this script can make without turning preflight into a
# network/API validation step.
for key in R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do
  value=$(grep -E "^${key}=" "$ENV_FILE" | cut -d= -f2-)
  [[ -n "$value" ]] || fail "$key is missing from $ENV_FILE"
  case "$value" in
    replace-me|REPLACE_ME|changeme|CHANGEME|placeholder|PLACEHOLDER|\<*|\>*|...)
      fail "$key still contains a placeholder value"
      ;;
  esac
done
grep -Fq "endpoint = \"https://$R2_ALLOWED_HOST\"" "$ROOT/config/openab.toml" ||
  fail "config/openab.toml [filestore].endpoint must be https://$R2_ALLOWED_HOST"

# STT 是選配，且不是公司 gateway 的延伸假設。兩個值要嘛都沒有、要嘛都填；真正啟用
# 前還必須把 OpenAB config 的 [stt].enabled 改成 true。
stt_base=$(grep -E '^STT_BASE_URL=' "$ENV_FILE" || true)
stt_key=$(grep -E '^STT_API_KEY=' "$ENV_FILE" || true)
if [[ -n ${stt_base#STT_BASE_URL=} || -n ${stt_key#STT_API_KEY=} ]]; then
  [[ -n ${stt_base#STT_BASE_URL=} && -n ${stt_key#STT_API_KEY=} ]] ||
    fail "STT_BASE_URL and STT_API_KEY must be set together"
  [[ $stt_base == STT_BASE_URL=https://* ]] || fail "STT_BASE_URL must be an https URL"
fi

printf 'runtime: codex-acp\nconfig:  %s\n' "$ROOT/config/openab.toml"
# A catalog that does not parse stops codex from starting at all.
python3 -c 'import tomllib,sys; tomllib.load(open(sys.argv[1],"rb"))' "$ROOT/config/codex/config.toml" ||
  fail "config/codex/config.toml is not valid TOML"
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["models"]' "$ROOT/config/codex/catalog.json" ||
  fail "config/codex/catalog.json is not a valid model catalog"
# The bot joins this external network with the one IP the firewall lets reach the proxy.
docker network inspect work-agent-net >/dev/null 2>&1 ||
  fail "docker network work-agent-net missing (run scripts/install-host-network.sh install)"

# The container runs as HOST_UID:HOST_GID (see Dockerfile), so the writable
# bind mounts must belong to that identity.
[[ $(stat -c '%u:%g' "$STATE_DIR") == "$HOST_UID:$HOST_GID" ]] || fail "$STATE_DIR must be owned by $HOST_UID:$HOST_GID"
# thread workspace（bot 寫）與 sandbox out（sandbox 以 node 寫、bot 唯讀）。不存在時 Docker 會
# 自己建成 root 擁有，兩邊都寫不進去，所以這裡先擋。
for dir in "$ROOT/runtime/work" "$ROOT/runtime/out"; do
  [[ -d $dir ]] || fail "missing $dir (mkdir -p it as $HOST_UID:$HOST_GID before compose up)"
  [[ $(stat -c '%u:%g' "$dir") == "$HOST_UID:$HOST_GID" ]] || fail "$dir must be owned by $HOST_UID:$HOST_GID"
done
[[ $(stat -c '%u:%g' "$DRAFT_DIR") == "$HOST_UID:$HOST_GID" ]] || fail "$DRAFT_DIR must be owned by $HOST_UID:$HOST_GID"
[[ $(id -u) == "$HOST_UID" ]] || fail "HOST_UID=$HOST_UID does not match the user running docker ($(id -u))"

# The whole snapshot root is mounted at /home/node/code, so nothing but the configured
# snapshots may live in it.
[[ -d "$SNAPSHOT_ROOT" ]] || fail "snapshot root missing: $SNAPSHOT_ROOT (run scripts/install-sync-cron.sh)"
[[ -d "$SNAPSHOT_ROOT/agent-config/skills" ]] || fail "missing $SNAPSHOT_ROOT/agent-config/skills (skills are mounted from there)"
# Docker would create a missing bind source as root and the container could not
# write the index, so fail loudly instead.
[[ -d "$SNAPSHOT_ROOT/.index" ]] || fail "missing $SNAPSHOT_ROOT/.index (run scripts/update-snapshots.sh)"
[[ $(stat -c '%u:%g' "$SNAPSHOT_ROOT/.index") == "$HOST_UID:$HOST_GID" ]] || fail "$SNAPSHOT_ROOT/.index must be owned by $HOST_UID:$HOST_GID"

declare -A expected=()
while IFS='|' read -r name _ branch; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  expected[$name]=1
  repo="$SNAPSHOT_ROOT/$name"
  [[ -d "$repo/.git" ]] || fail "snapshot missing: $repo"
  [[ $(git -C "$repo" branch --show-current) == "$branch" ]] || fail "$name is not on $branch"
  [[ -z $(git -C "$repo" status --porcelain) ]] || fail "$name snapshot is dirty"
done < "$ROOT/config/repos.conf"

for entry in "$SNAPSHOT_ROOT"/*; do
  [[ -e "$entry" ]] || continue
  name=$(basename "$entry")
  [[ -n ${expected[$name]:-} ]] || fail "$entry is not listed in config/repos.conf but would be mounted"
done

# These two external controls cannot be checked through Slack or Cloudflare
# APIs. A human must confirm them on the actual deployment before preflight can
# pass. Requiring a TTY also makes noninteractive deploys fail closed.
require_manual_release_gate() {
  [[ -t 0 && -t 1 && -r /dev/tty ]] ||
    fail "manual release gate requires an interactive terminal; refusing noninteractive deployment"

  printf '\nManual release gate (no API verification is attempted):\n'
  printf '  [ ] Cloudflare R2 bucket work-agent-attachments has a 1-day lifecycle expiration for incoming/\n'
  printf '  [ ] Slack app has files:read and was Reinstall to Workspace after the scope change\n'
  local lifecycle slack
  read -r -p 'Confirm the R2 lifecycle check [y/N]: ' lifecycle </dev/tty
  [[ $lifecycle == y || $lifecycle == Y ]] || fail "R2 lifecycle manual gate was not confirmed"
  read -r -p 'Confirm the Slack files:read check [y/N]: ' slack </dev/tty
  [[ $slack == y || $slack == Y ]] || fail "Slack files:read manual gate was not confirmed"
  printf 'Manual release gate confirmed.\n'
}

require_manual_release_gate

docker compose -f "$ROOT/compose.yaml" config --quiet
printf 'Preflight passed.\n'
