#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib.sh
source "$ROOT/scripts/lib.sh"
load_env "$ROOT"
VERSION_FILE="$ROOT/config/versions.env"
version_keys=(CODEX_VERSION CODEX_ACP_VERSION CODEGRAPH_VERSION)
declare -A pinned_versions=()
[[ -r "$VERSION_FILE" ]] || { printf 'FAIL: missing %s\n' "$VERSION_FILE" >&2; exit 1; }
while IFS='=' read -r key value; do
  case "$key" in
    CODEX_VERSION|CODEX_ACP_VERSION|CODEGRAPH_VERSION)
      [[ "$value" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
        printf 'FAIL: invalid %s in config/versions.env\n' "$key" >&2
        exit 1
      }
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
compose() { docker compose -f "$ROOT/compose.yaml" "$@"; }

"$ROOT/scripts/preflight.sh"
render_codex_config "$ROOT/config/codex/config.toml" "$ROOT/env/openab.env" "$ROOT/runtime/codex/config.toml"
compose build --pull
compose up -d --remove-orphans

compose exec -T agent python3 --version
compose exec -T agent git --version
compose exec -T agent /home/node/snapshots/agent-config/skills/slack-list/scripts/slack-list --help >/dev/null
# Home is writable like OpenAB's official PVC; the snapshot on top of it is not.
compose exec -T agent sh -lc \
  'test -w /home/node &&
   touch /home/node/.deploy-write-probe && rm /home/node/.deploy-write-probe &&
   test ! -w /home/node/snapshots && test -w /home/node/snapshots/.index'
# agent-sync ran in pre_boot: skills and MCP came from agent-config through skillshare, the
# rules are the maintainer's plus the bot section, and the provider has a real gateway URL.
compose exec -T agent sh -lc \
  'test -e /home/node/.agents/skills/slack-list/SKILL.md &&
   grep -q "^\[mcp_servers\.codegraph\]" /home/node/.codex/config.toml &&
   grep -q "^\[model_providers\.company\]" /home/node/.codex/config.toml &&
   ! grep -qF "\${COMPANY_GATEWAY_BASE_URL}" /home/node/.codex/config.toml &&
   grep -qF "company agent" /home/node/.codex/AGENTS.md &&
   test -r /etc/codex/config.toml && test ! -w /etc/codex/config.toml &&
   test ! -w /etc/codex/AGENTS.bot.md && test ! -w /home/node/.config/skillshare/config.yaml'
# The chrome sidecar shares the bot's localhost: the same --browser-url the maintainer uses.
compose exec -T agent sh -lc "playwright-cli --version >/dev/null"
compose exec -T agent curl -fsS --retry 10 --retry-connrefused --retry-delay 1 --max-time 30 http://127.0.0.1:9222/json/version >/dev/null

# 生圖與上傳：只驗它們在、可執行、不可寫。這裡不對 gateway 或 Slack 發任何請求。
compose exec -T agent sh -lc \
  'command -v company-image >/dev/null &&
    company-image --help >/dev/null &&
    slack-thread-artifact --help >/dev/null &&
    test ! -w /usr/local/bin/slack-thread-artifact &&
    test ! -w /usr/local/bin/company-image'
# Retired runtimes must not be reachable from the agent.
compose exec -T agent sh -lc \
  '! command -v opencode && ! command -v claude && ! command -v claude-agent-acp'

# Codex runtime: the pinned codex/codex-acp under /opt/codex-<ver>, the CODEX_PATH
# OpenAB hands to codex-acp, and the rendered provider config read-only on top of CODEX_HOME.
# Versions and paths only; no Slack or gateway call is made here.
compose exec -T agent sh -lc \
  "test \"\$(readlink -f \"\$(command -v codex-acp)\")\" = \"\$(readlink -f /opt/codex-$CODEX_VERSION/node_modules/.bin/codex-acp)\" &&
   test \"\$(/opt/codex-$CODEX_VERSION/node_modules/.bin/codex --version)\" = 'codex-cli $CODEX_VERSION' &&
   test \"\$(node -p 'require(process.argv[1]).version' /opt/codex-$CODEX_VERSION/node_modules/@agentclientprotocol/codex-acp/package.json)\" = '$CODEX_ACP_VERSION' &&
   grep -qF 'CODEX_PATH = \"/opt/codex-$CODEX_VERSION/node_modules/.bin/codex\"' /etc/openab/config.toml &&
   grep -q '^command = \"codex-acp\"' /etc/openab/config.toml"
compose exec -T agent sh -lc \
  'test -w /home/node/.codex && test -r /etc/codex/catalog.json && test ! -w /etc/codex/catalog.json'
compose exec -T agent sh -lc \
  "codegraph --version | grep -qF '$CODEGRAPH_VERSION'"

compose ps

# preflight.sh 在 build 前已完成 R2 secret/host 檢查，並以 interactive gate 確認 lifecycle
# 與 Slack files:read；若沒有 TTY 或任一項未確認，deploy 在這裡就已中止。這支跑完仍不等於
# 這一版可以放給人用。上面每一條都是 container 內的存在性與權限檢查，
# 沒有一條打過 Slack、公司 gateway 或 STT，也沒有一條證明排程真的會觸發。
# 那些只能由人在有 secret 的環境做一次，做完才算 release。
cat <<'GATE'

Deploy finished. What it just verified: the container starts, the pinned codex and
codex-acp are the ones config/versions.env records, agent-sync synced the provider,
rules, skills and MCP from agent-config, the Chrome sidecar answers on 127.0.0.1:9222,
the retired runtimes are gone, and home is writable while the snapshot is not. Preflight also confirmed the R2 host and credentials
are present and recorded the two manual prerequisite confirmations.

NOT VERIFIED HERE — manual release gate, see the "人工 release gate" section of
docs/runbook.md. Do not call this version released until every line is ticked off:
  1. Slack round trip: an authorised user gets an answer; an unauthorised one does not.
  2. slack-thread-artifact against real Slack: file lands in the original thread and
     the draft is deleted; force one failure and confirm the draft is kept.
  3. company-image against the real company gateway (this bills one call).
  4. Artifact cleanup: `./scripts/cleanup-artifacts.sh` by hand, then confirm the
     crontab entry exists and its log has no FAILED line.
  5. STT, only if [stt].enabled is true: one real audio file through the endpoint.
  6. Model inference straight to the company gateway: one Slack question gets an
     answer with no "Model metadata" warning, a shell tool call works, and a second
     @mention in the same thread resumes the session (session/load) instead of
     starting over. Nothing offline can prove the gateway accepts this route.
  7. Coding loop: ask for a small change in one repo; the agent clones from
     /home/node/snapshots, runs `pnpm install`, and uploads a .patch to the thread.
  8. Browser: ask the agent to start that repo's dev server and send a screenshot
     taken through chrome-devtools; the PNG lands in the thread.
GATE
