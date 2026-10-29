#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT"
VERSION_FILE="$ROOT/config/versions.env"
version_keys=(CODEX_VERSION CODEX_ACP_VERSION CODEGRAPH_VERSION PLAYWRIGHT_VERSION)
declare -A pinned_versions=()
[[ -r "$VERSION_FILE" ]] || { printf 'FAIL: missing %s\n' "$VERSION_FILE" >&2; exit 1; }
while IFS='=' read -r key value; do
  case "$key" in
    CODEX_VERSION|CODEX_ACP_VERSION|CODEGRAPH_VERSION|PLAYWRIGHT_VERSION)
      [[ "$value" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
        printf 'FAIL: invalid %s in config/versions.env\n' "$key" >&2
        exit 1
      }
      pinned_versions["$key"]=$value
      ;;
    SANDBOX_NODE_IMAGE)
      pinned_versions["$key"]=$value
      ;;
  esac
done < "$VERSION_FILE"
for key in "${version_keys[@]}"; do
  value=${pinned_versions[$key]:-}
  [[ -n "$value" ]] || { printf 'FAIL: missing %s in config/versions.env\n' "$key" >&2; exit 1; }
  if [[ -z ${!key:-} ]]; then
    printf -v "$key" '%s' "$value"
    export "$key"
  fi
  [[ ${!key} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    printf 'FAIL: invalid version override for %s\n' "$key" >&2
    exit 1
  }
done
# The sandbox base image must be an exact tag pinned by digest (spec #21 §7).
[[ -n ${SANDBOX_NODE_IMAGE:-} ]] || SANDBOX_NODE_IMAGE=${pinned_versions[SANDBOX_NODE_IMAGE]:-}
[[ $SANDBOX_NODE_IMAGE =~ ^node:[0-9]+\.[0-9]+\.[0-9]+-bookworm@sha256:[0-9a-f]{64}$ ]] || {
  printf 'FAIL: SANDBOX_NODE_IMAGE must be node:<x.y.z>-bookworm@sha256:<digest>\n' >&2
  exit 1
}
export SANDBOX_NODE_IMAGE
compose() { docker compose -f "$ROOT/compose.yaml" "$@"; }
# The proxy address the bot must see; derived from the network source of truth rather
# than copied here (compose.yaml cannot read that file, so tests/static.sh compares it).
NETWORK_FILE="$ROOT/config/host-network.env"
proxy_ip=$(read_env_value "$NETWORK_FILE" WORK_AGENT_GATEWAY_IP) &&
  proxy_port=$(read_env_value "$NETWORK_FILE" WORK_AGENT_PROXY_PORT) || {
  printf 'FAIL: WORK_AGENT_GATEWAY_IP/WORK_AGENT_PROXY_PORT missing in %s\n' "$NETWORK_FILE" >&2
  exit 1
}
PROXY_URL="http://$proxy_ip:$proxy_port"

"$ROOT/scripts/preflight.sh"
compose build --pull
compose up -d --remove-orphans

compose exec -T backlog-agent python3 --version
compose exec -T backlog-agent git --version
compose exec -T backlog-agent /home/node/code/agent-config/skills/slack-list/scripts/slack-list --help >/dev/null
compose exec -T backlog-agent sh -lc 'test -w /home/node/.openab && test -w /home/node/drafts'
compose exec -T backlog-agent sh -lc 'test -d /home/node/.agents/skills/slack-list && test ! -w /home/node/.agents/skills && test ! -w /home/node/code'
# Home stays read-only. CODEX_HOME is the one named volume Codex needs; widening
# /home/node instead would quietly make the read-only config, skills and snapshot
# mounts underneath it writable.
compose exec -T backlog-agent sh -lc 'test ! -w /home/node'

# 生圖路徑：只驗它在、可執行、不可寫。這裡不對 gateway 發任何請求。
compose exec -T backlog-agent sh -lc \
  'command -v company-image >/dev/null &&
    company-image --help >/dev/null &&
    slack-thread-artifact --help >/dev/null &&
    test ! -w /usr/local/bin/slack-thread-artifact &&
    test ! -w /usr/local/bin/company-image'
# The gateway key lives only in the host proxy; the bot reaches the gateway through
# the proxy address. `env` is the container env every agent process starts from.
# shellcheck disable=SC2016 # $COMPANY_GATEWAY_BASE_URL is expanded inside the container
compose exec -T backlog-agent sh -lc \
  'test "$COMPANY_GATEWAY_BASE_URL" = "$1" &&
   ! env | grep -q "^COMPANY_GATEWAY_API_KEY="' sh "$PROXY_URL"
# Retired runtimes and GitHub CLI must not be reachable from the agent.
compose exec -T backlog-agent sh -lc \
  '! command -v gh && ! command -v opencode && ! command -v claude && ! command -v claude-agent-acp'

# Codex runtime: the pinned codex/codex-acp under /opt/codex-<ver>, the CODEX_PATH
# OpenAB hands to codex-acp, and CODEX_HOME writable with read-only config on top.
# Versions and paths only; no Slack or gateway call is made here.
compose exec -T backlog-agent sh -lc \
  "test \"\$(readlink -f \"\$(command -v codex-acp)\")\" = \"\$(readlink -f /opt/codex-$CODEX_VERSION/node_modules/.bin/codex-acp)\" &&
   test \"\$(/opt/codex-$CODEX_VERSION/node_modules/.bin/codex --version)\" = 'codex-cli $CODEX_VERSION' &&
   test \"\$(node -p 'require(process.argv[1]).version' /opt/codex-$CODEX_VERSION/node_modules/@agentclientprotocol/codex-acp/package.json)\" = '$CODEX_ACP_VERSION' &&
   grep -qF 'CODEX_PATH = \"/opt/codex-$CODEX_VERSION/node_modules/.bin/codex\"' /etc/openab/config.toml &&
   grep -q '^command = \"codex-acp\"' /etc/openab/config.toml"
compose exec -T backlog-agent sh -lc \
  'test -w /home/node/.codex &&
   touch /home/node/.codex/.deploy-write-probe &&
   rm /home/node/.codex/.deploy-write-probe &&
   test -r /home/node/.codex/config.toml && test ! -w /home/node/.codex/config.toml &&
   test -r /home/node/.codex/AGENTS.md && test ! -w /home/node/.codex/AGENTS.md &&
   test -r /etc/codex/catalog.json && test ! -w /etc/codex/catalog.json'
compose exec -T backlog-agent sh -lc \
  "codegraph --version | grep -qF '$CODEGRAPH_VERSION'"

compose ps

# preflight.sh 在 build 前已完成 R2 secret/host 檢查，並以 interactive gate 確認 lifecycle
# 與 Slack files:read；若沒有 TTY 或任一項未確認，deploy 在這裡就已中止。這支跑完仍不等於
# 這一版可以放給人用。上面每一條都是 container 內的存在性與權限檢查，
# 沒有一條打過 Slack、公司 gateway 或 STT，也沒有一條證明排程真的會觸發。
# 那些只能由人在有 secret 的環境做一次，做完才算 release。
cat <<'GATE'

Deploy finished. What it just verified: the container starts, the pinned codex and
codex-acp are the ones config/versions.env records, the gateway key is not in the
container, the GitHub CLI and the retired runtimes are gone, and CODEX_HOME is
writable under a read-only config. Preflight also confirmed the R2 host
and credentials are present and recorded the two manual prerequisite confirmations.

NOT VERIFIED HERE — manual release gate, see the "人工 release gate" section of
docs/runbook.md. Do not call this version released until every line is ticked off:
  1. Slack round trip: an authorised user gets an answer; an unauthorised one does not.
  2. slack-thread-artifact against real Slack: file lands in the original thread and
     the draft is deleted; force one failure and confirm the draft is kept.
  3. company-image against the real company gateway (this bills one call).
  4. Artifact cleanup: `./scripts/cleanup-artifacts.sh` by hand, then confirm the
     crontab entry exists and its log has no FAILED line.
  5. STT, only if [stt].enabled is true: one real audio file through the endpoint.
  6. Model inference through OpenAB and the host proxy: one Slack question gets an
     answer with no "Model metadata" warning, a shell tool call works, and a second
     @mention in the same thread resumes the session (session/load) instead of
     starting over. Nothing offline can prove the gateway accepts this route.
GATE
