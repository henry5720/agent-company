#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LIMITED=()
limited() { LIMITED+=("$1"); }

for path in CONTEXT.md CLAUDE.md Dockerfile compose.yaml .env.example \
  docs/system-design.md README.md config/openab.toml config/versions.env \
  config/slack-home.json env/openab.env.example; do
  test -f "$ROOT/$path"
done
test ! -e "$ROOT/config/openab.claude-acp.toml"
test ! -e "$ROOT/scripts/compose.sh"
test -x "$ROOT/agents/bin/company-image"
test -x "$ROOT/agents/bin/slack-thread-artifact"
for retired in config/opencode managed-claude-settings.json agents/bin/parse-document \
  tests/parse-document.py tests/provider-route.py; do
  test ! -e "$ROOT/$retired" || { echo "retired runtime file still present: $retired" >&2; exit 1; }
done
test -f "$ROOT/config/codex/config.toml"
test -f "$ROOT/config/codex/catalog.json"
test -x "$ROOT/agents/bin/sandbox-run"
test -x "$ROOT/sandbox/runner"
test -x "$ROOT/sandbox/sandbox-screenshot"
test -x "$ROOT/sandbox/sandbox-attachment"
test -x "$ROOT/agents/bin/thread-workspace"
test -x "$ROOT/scripts/host-firewall.sh"
test -x "$ROOT/scripts/install-host-network.sh"

python3 - "$ROOT" <<'PY'
import json, os, pathlib, re, subprocess, tomllib
root = pathlib.Path(__import__('sys').argv[1])
cfg = tomllib.loads((root / 'config/openab.toml').read_text())
assert cfg['slack']['allow_all_users'] is True
# spec #21 §4：OpenAB 直接跑 codex-acp，三個 env 只從 [agent].env 給。
assert cfg['agent']['command'] == 'codex-acp', cfg['agent']['command']
assert not cfg['agent'].get('args'), 'codex-acp takes no args'
assert cfg['agent']['working_dir'] == '/home/node/code'
inherit = cfg['agent']['inherit_env']
for key in ('COMPANY_GATEWAY_BASE_URL', 'SLACK_BOT_TOKEN'):
    assert key in inherit, key
for key in inherit:
    assert key != 'COMPANY_GATEWAY_API_KEY', 'the gateway key lives only in the host proxy'
    assert not key.startswith(('PARSE_DOCUMENT_', 'DOCLING_', 'OPENCODE_', 'CLAUDE_')), key
assert cfg['filestore']['max_file_size_mb'] == 50

versions = {}
for line in (root / 'config/versions.env').read_text().splitlines():
    if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', line):
        k, v = line.split('=', 1); versions[k] = v
for forbidden in ('OPENAB_AGENT_RUNTIME', 'OPENAB_IMAGE_OPENCODE', 'OPENAB_IMAGE_CLAUDE'):
    assert forbidden not in versions
assert '@sha256:' in versions['OPENAB_IMAGE']
tag = versions['OPENAB_IMAGE'].split('@', 1)[0].rsplit(':', 1)[-1]
assert tag not in ('latest', 'beta', 'stable')
for key in ('CODEX_VERSION', 'CODEX_ACP_VERSION', 'CODEGRAPH_VERSION',
            'PLAYWRIGHT_VERSION', 'OFFICEPARSER_VERSION'):
    assert re.fullmatch(r'\d+\.\d+\.\d+', versions[key]), key
for retired in ('OPENCODE_VERSION', 'OMO_VERSION', 'CLAUDE_AGENT_ACP_VERSION',
                'CLAUDE_CODE_VERSION', 'DOCLING_VERSION'):
    assert retired not in versions, f'{retired} belongs to the retired runtime'
assert re.fullmatch(r'ghcr\.io/openabdev/openab:[0-9.]+(-beta\.\d+)?-codex@sha256:[0-9a-f]{64}', versions['OPENAB_IMAGE']), \
    'bot image must be the OpenAB codex image pinned by digest'
CODEX_PREFIX = f"/opt/codex-{versions['CODEX_VERSION']}"
CODEX_PATH = f'{CODEX_PREFIX}/node_modules/.bin/codex'
agent_env = cfg['agent'].get('env', {})
assert agent_env == {
    'CODEX_HOME': '/home/node/.codex',
    'CODEX_PATH': CODEX_PATH,
    'INITIAL_AGENT_MODE': 'agent-full-access',
}, agent_env

dockerfile = (root / 'Dockerfile').read_text()
assert 'ARG CODEX_VERSION\n' in dockerfile and 'ARG CODEX_ACP_VERSION\n' in dockerfile
assert 'npm install --prefix "$CODEX_PREFIX"' in dockerfile and 'CODEX_PREFIX="/opt/codex-${CODEX_VERSION}"' in dockerfile, \
    'codex and codex-acp are installed together under /opt/codex-<ver>'
assert 'apt-get purge -y gh' in dockerfile, 'upstream installs gh; the bot must not have it'
installs = '\n'.join(l for l in dockerfile.lower().splitlines() if '! command -v' not in l)
for retired in ('opencode', 'oh-my-opencode', 'claude-agent-acp', 'claude-code', 'docling', 'parse-document'):
    assert retired not in installs, f'Dockerfile still mentions {retired}'

# Codex config（spec #21 §4、#24 決定 7）：provider 經 host proxy、只開 Responses API。
host_net = {}
for line in (root / 'config/host-network.env').read_text().splitlines():
    if re.match(r'^[A-Z_][A-Z0-9_]*=', line):
        k, v = line.split('=', 1); host_net[k] = v
PROXY = f"http://{host_net['WORK_AGENT_GATEWAY_IP']}:{host_net['WORK_AGENT_PROXY_PORT']}"
codex = tomllib.loads((root / 'config/codex/config.toml').read_text())
assert codex['model_provider'] == 'company'
provider = codex['model_providers']['company']
assert provider['wire_api'] == 'responses', 'the company gateway only serves the Responses API'
assert provider['base_url'] == PROXY, provider['base_url']
assert 'env_key' not in provider and 'experimental_bearer_token' not in provider, 'the key is injected by the proxy'
assert codex['model_catalog_json'] == '/etc/codex/catalog.json'
catalog = json.loads((root / 'config/codex/catalog.json').read_text())
assert [m['slug'] for m in catalog['models']] == [codex['model']], 'catalog must hold exactly the configured model'
assert catalog['models'][0]['context_window'] > 0
assert re.fullmatch(r'node:\d+\.\d+\.\d+-bookworm@sha256:[0-9a-f]{64}', versions['SANDBOX_NODE_IMAGE']), \
    'SANDBOX_NODE_IMAGE must be an exact node bookworm tag with a digest'
sandbox_dockerfile = (root / 'sandbox/Dockerfile').read_text()
assert 'ARG SANDBOX_NODE_IMAGE\nFROM ${SANDBOX_NODE_IMAGE}\n' in sandbox_dockerfile
assert 'ARG PLAYWRIGHT_VERSION\n' in sandbox_dockerfile, 'Playwright version must come from versions.env'
assert 'install --with-deps chromium' in sandbox_dockerfile
assert 'ARG OFFICEPARSER_VERSION\n' in sandbox_dockerfile and 'OFFICEPARSER_VERSION=${OFFICEPARSER_VERSION}' in sandbox_dockerfile, \
    'attachment converter version must come from versions.env and reach the runtime env'
assert 'npm install -g "playwright@' in sandbox_dockerfile and not re.search(r'npm install[^\n]*officeparser', sandbox_dockerfile), \
    'officeparser is installed on the fly, not baked into the sandbox image'
assert re.search(r'^USER node$', sandbox_dockerfile, re.M) and sandbox_dockerfile.rstrip().splitlines()[-1] == 'CMD ["sandbox-runner"]'
assert sandbox_dockerfile.rfind('USER node') > sandbox_dockerfile.rfind('USER root'), 'sandbox must run as node'
assert not re.search(r'(TOKEN|API_KEY|SECRET)', sandbox_dockerfile), 'sandbox image must not carry tokens'
assert not re.search(r'@(latest|beta|stable|next)\b', sandbox_dockerfile)

repos = {}
for line in (root / 'config/repos.conf').read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    name, clone_url, branch = line.split('|')
    repos[name] = (clone_url, branch)
assert repos['teamsync-tutorials'] == ('git@github.com:ShuChenAI/teamsync-tutorials.git', 'main')
assert repos['teamsync-app'] == ('git@github.com:ShuChenAI/teamsync-app.git', 'main')
home = (root / 'config/slack-home.json').read_text()
assert all(name in home for name in repos), 'repos.conf names must appear in Slack Home JSON'

deploy = (root / 'scripts/deploy.sh').read_text()
assert 'VERSION_FILE="$ROOT/config/versions.env"' in deploy
assert "while IFS='=' read -r key value; do" in deploy
assert 'done < "$VERSION_FILE"' in deploy
assert 'source "$VERSION_FILE"' not in deploy and 'eval ' not in deploy
assert 'version_keys=(CODEX_VERSION CODEX_ACP_VERSION CODEGRAPH_VERSION PLAYWRIGHT_VERSION)' in deploy
assert 'CODEX_VERSION|CODEX_ACP_VERSION|CODEGRAPH_VERSION|PLAYWRIGHT_VERSION)' in deploy
assert '[[ -z ${!key:-} ]]' in deploy, 'pre-set version environment values must be preserved'
loader_start = deploy.index('VERSION_FILE=')
loader_end = deploy.index('compose() {', loader_start)
loader = deploy[loader_start:loader_end]
version_keys = ('CODEX_VERSION', 'CODEX_ACP_VERSION', 'CODEGRAPH_VERSION', 'PLAYWRIGHT_VERSION', 'SANDBOX_NODE_IMAGE')
print_versions = 'printf "%s\\n" ' + ' '.join(f'"${{{key}}}"' for key in version_keys)
clean_env = {'PATH': os.environ.get('PATH', '')}
def run_loader(extra):
    return subprocess.run(
        ['bash', '-c', 'set -euo pipefail; ROOT="$1"; ' + loader + print_versions, 'test', str(root)],
        env={**clean_env, **extra}, capture_output=True, text=True,
    )
loaded = run_loader({})
assert loaded.returncode == 0 and loaded.stdout.splitlines() == [versions[key] for key in version_keys], \
    'clean deploy environment must load pinned versions: ' + loaded.stderr
overridden = run_loader({'CODEX_VERSION': '9.8.7'}).stdout.splitlines()
assert overridden == ['9.8.7', *[versions[key] for key in version_keys[1:]]], 'explicit version override must be preserved'
assert run_loader({'SANDBOX_NODE_IMAGE': 'node:22-bookworm'}).returncode != 0, 'SANDBOX_NODE_IMAGE without a digest must fail'

snapshot_updater = (root / 'scripts/update-snapshots.sh').read_text()
assert 'SNAPSHOT_ROOT="$ROOT" docker compose -f "$REPO_DIR/compose.yaml" config --images backlog-agent' in snapshot_updater
# bot 釘了 ipv4_address，`compose run` 第二個 container 會撞 IP；索引改用不連網的 docker run。
assert 'docker compose -f "$REPO_DIR/compose.yaml" run' not in snapshot_updater
assert 'docker compose -f "$ROOT/compose.yaml" run' not in snapshot_updater
assert 'docker run --rm --network none' in snapshot_updater

compose = (root / 'compose.yaml').read_text()
assert '${OPENAB_IMAGE' not in compose
assert '${OPENAB_AGENT_RUNTIME' not in compose
assert '${OPENAB_CONFIG' not in compose
assert 'openab.toml:/etc/openab/config.toml' in compose
assert '@sha256:' in compose
assert ':latest' not in compose and ':stable' not in compose

import yaml
doc = yaml.safe_load(compose)
bot = doc['services']['backlog-agent']
assert bot['build']['args'] == {
    'OPENAB_IMAGE': versions['OPENAB_IMAGE'],
    'CODEX_VERSION': versions['CODEX_VERSION'],
    'CODEX_ACP_VERSION': versions['CODEX_ACP_VERSION'],
    'CODEGRAPH_VERSION': versions['CODEGRAPH_VERSION'],
    'HOST_UID': '${HOST_UID:-1000}',
    'HOST_GID': '${HOST_GID:-1000}',
}, 'bot build args must equal config/versions.env'
# spec #21 §8
assert bot['mem_limit'] == '6g' and float(bot['cpus']) == 4.0 and bot['pids_limit'] == 1024
env = bot.get('environment', {})
assert 'COMPANY_GATEWAY_API_KEY' not in env, 'the gateway key must not reach the bot container'
assert env.get('COMPANY_GATEWAY_BASE_URL') == PROXY, 'company-image and Codex both go through the host proxy'
example = (root / 'env/openab.env.example').read_text()
assert not re.search(r'^COMPANY_GATEWAY_(API_KEY|BASE_URL)=', example, re.M), \
    'env/openab.env is loaded with env_file; gateway settings must not come from it'
assert not any(k.startswith(('PARSE_DOCUMENT_', 'DOCLING_', 'OPENCODE_', 'CLAUDE_')) for k in env), env
mounts = bot['volumes']
assert 'codex-home:/home/node/.codex' in mounts, 'CODEX_HOME must be a named volume or session/load fails'
assert 'codex-home' in doc['volumes']
assert './config/codex/config.toml:/home/node/.codex/config.toml:ro,z' in mounts
assert './config/codex/catalog.json:/etc/codex/catalog.json:ro,z' in mounts
assert '${SNAPSHOT_ROOT:?set SNAPSHOT_ROOT in .env}:/home/node/code:ro,z' in mounts
assert '${SNAPSHOT_ROOT:?set SNAPSHOT_ROOT in .env}/agent-config/skills:/home/node/.agents/skills:ro,z' in mounts
assert './agents/CLAUDE.md:/home/node/.codex/AGENTS.md:ro,z' in mounts, 'Codex reads the runtime contract as global AGENTS.md'
assert not any('opencode' in m or '.claude' in m for m in mounts), mounts
assert not any('docker.sock' in m for m in mounts) and not bot.get('privileged')
assert bot['networks'] == {host_net['WORK_AGENT_NETWORK']: {'ipv4_address': host_net['WORK_AGENT_BOT_IP']}}, \
    'bot must hold the one IP the firewall lets reach the proxy'
assert doc['networks'][host_net['WORK_AGENT_NETWORK']] == {'external': True}
PY

grep -Fq 'allow_all_users = true' "$ROOT/config/openab.toml"
grep -Fq 'WORK_HELPER_ISSUE_MODE=manual' "$ROOT/env/openab.env.example"
grep -Fq 'require_manual_release_gate' "$ROOT/scripts/preflight.sh"
grep -Fq 'docker compose -f "$ROOT/compose.yaml"' "$ROOT/scripts/deploy.sh"
grep -Fq 'docker compose -f "$ROOT/compose.yaml"' "$ROOT/scripts/preflight.sh"
python3 - "$ROOT/scripts/publish-slack-home.sh" <<'PY'
import pathlib, sys
script = pathlib.Path(sys.argv[1]).read_text()
for required in ('users.list', 'next_cursor', 'cursor', '.ok == true',
                 '.deleted != true', '.is_bot != true', '.is_app_user != true',
                 'No eligible workspace users', 'Published:'):
    assert required in script, f'publish-slack-home.sh is missing {required!r}'
assert 'allowed_users' not in script, 'Home publishing must not read nonexistent allowed_users'
PY
! grep -R -n -E 'scripts/compose\.sh|openab\.claude-acp|OPENAB_AGENT_RUNTIME|OPENAB_IMAGE_(OPENCODE|CLAUDE)' \
  "$ROOT/scripts" "$ROOT/compose.yaml" "$ROOT/config" "$ROOT/README.md" "$ROOT/CLAUDE.md" "$ROOT/docs"
# 退場的 runtime 不得殘留在 bot 的機器設定裡。文件與 Slack Home 文案由 #34 一起改。
# 註解與 deploy.sh 裡斷言「不存在」的那幾行（`! command -v ...`）不算殘留。
# 注意：`! grep` 在 set -e 底下不會讓 script 失敗，所以這裡用 if。
if grep -R -n -i -E 'opencode|oh-my-opencode|claude-agent-acp|parse-document|docling' \
  --exclude=slack-home.json \
  "$ROOT/scripts" "$ROOT/compose.yaml" "$ROOT/Dockerfile" "$ROOT/config" "$ROOT/env/openab.env.example" |
  grep -v -E '^[^:]+:[0-9]+:[[:space:]]*#|! command -v'; then
  echo 'retired runtime (OpenCode/OMO/Claude ACP/parse-document/Docling) still referenced' >&2
  exit 1
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  (cd "$ROOT" && docker compose config --quiet)
else
  limited 'docker compose config --quiet (Docker unavailable)'
fi

"$ROOT/tests/image-runtime.py"
"$ROOT/tests/slack-thread-artifact.py"
"$ROOT/tests/artifact-path-safety.py"
"$ROOT/tests/sandbox-runner.py"
"$ROOT/tests/sandbox-run.py"
"$ROOT/tests/sandbox-attachment.py"
"$ROOT/tests/thread-workspace.py"
"$ROOT/tests/host-network.py"
if [[ -z ${CADDY_BIN:-} ]] && ! command -v caddy work-agent-caddy >/dev/null 2>&1; then
  limited 'host proxy live route test (no caddy binary; install-host-network.sh puts work-agent-caddy on PATH)'
fi

if ((${#LIMITED[@]})); then
  printf 'LIMITED VERIFICATION\n'
  printf ' - %s\n' "${LIMITED[@]}"
fi
printf 'Static checks passed.\n'
