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
test -x "$ROOT/agents/bin/agent-sync"
test -f "$ROOT/config/skillshare/config.yaml"
for retired in config/opencode managed-claude-settings.json agents/bin/parse-document \
  tests/parse-document.py tests/provider-route.py agents/CLAUDE.md; do
  test ! -e "$ROOT/$retired" || { echo "retired runtime file still present: $retired" >&2; exit 1; }
done
test -f "$ROOT/config/codex/config.toml"
test -f "$ROOT/config/codex/catalog.json"
test -f "$ROOT/agents/AGENTS.md"

python3 - "$ROOT" <<'PY'
import json, os, pathlib, re, subprocess, tomllib
root = pathlib.Path(__import__('sys').argv[1])
cfg = tomllib.loads((root / 'config/openab.toml').read_text())
assert cfg['slack']['allow_all_users'] is True
# OpenAB 直接跑 codex-acp，三個 env 只從 [agent].env 給。
assert cfg['agent']['command'] == 'codex-acp', cfg['agent']['command']
assert not cfg['agent'].get('args'), 'codex-acp takes no args'
assert cfg['agent']['working_dir'] == '/home/node', 'OpenAB recommends home as the Codex working directory'
inherit = cfg['agent']['inherit_env']
for key in ('COMPANY_GATEWAY_BASE_URL', 'COMPANY_GATEWAY_API_KEY', 'SLACK_BOT_TOKEN'):
    assert key in inherit, key
assert 'CONTEXT7_API_KEY' in inherit, 'agent-config mcp.yaml reads context7 key from env'
# pre_boot runs agent-sync and blocks startup when it fails: without it Codex has no provider.
assert cfg['hooks']['pre_boot']['on_failure'] == 'abort'
assert 'exec /usr/local/bin/agent-sync' in cfg['hooks']['pre_boot']['inline']
for key in inherit:
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
for key in ('CODEX_VERSION', 'CODEX_ACP_VERSION', 'CODEGRAPH_VERSION', 'SKILLSHARE_VERSION', 'PLAYWRIGHT_CLI_VERSION'):
    assert re.fullmatch(r'\d+\.\d+\.\d+', versions[key]), key
for retired in ('OPENCODE_VERSION', 'OMO_VERSION', 'CLAUDE_AGENT_ACP_VERSION',
                'CLAUDE_CODE_VERSION', 'DOCLING_VERSION', 'SANDBOX_NODE_IMAGE',
                'PLAYWRIGHT_VERSION', 'OFFICEPARSER_VERSION', 'CADDY_VERSION'):
    assert retired not in versions, f'{retired} belongs to a retired component'
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
installs = '\n'.join(l for l in dockerfile.lower().splitlines() if '! command -v' not in l)
for retired in ('opencode', 'oh-my-opencode', 'claude-agent-acp', 'claude-code', 'docling', 'parse-document'):
    assert retired not in installs, f'Dockerfile still mentions {retired}'
# The home volume copies these from the image on first mount; missing ones come up root:root.
assert 'mkdir -p /home/node/.codex /home/node/.agents /home/node/snapshots /home/node/.local/bin' in dockerfile
assert '/home/node/.config/skillshare' in dockerfile, 'skillshare config mount point must be owned by node'
assert 'sha256sum -c' in dockerfile and 'ARG SKILLSHARE_LINUX_AMD64_SHA256' in dockerfile, 'skillshare download must be checksum-verified'
# agent is not root: global npm installs must land in home.
assert "prefix=/home/node/.local" in dockerfile and 'ENV PATH=/home/node/.local/bin:$PATH' in dockerfile

# Codex config：直連公司 gateway、只開 Responses API。位址由 deploy.sh 填進範本，key 走 env_key。
codex_template = (root / 'config/codex/config.toml').read_text()
assert codex_template.count('${COMPANY_GATEWAY_BASE_URL}') == 1
codex = tomllib.loads(codex_template)
assert codex['model_provider'] == 'company'
provider = codex['model_providers']['company']
assert provider['wire_api'] == 'responses', 'the company gateway only serves the Responses API'
assert provider['base_url'] == '${COMPANY_GATEWAY_BASE_URL}', provider['base_url']
assert provider['env_key'] == 'COMPANY_GATEWAY_API_KEY'
assert 'experimental_bearer_token' not in provider, 'the key must not be written into the config file'
assert codex['model_catalog_json'] == '/etc/codex/catalog.json'
catalog = json.loads((root / 'config/codex/catalog.json').read_text())
assert [m['slug'] for m in catalog['models']] == [codex['model']], 'catalog must hold exactly the configured model'
assert catalog['models'][0]['context_window'] > 0

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
assert 'version_keys=(CODEX_VERSION CODEX_ACP_VERSION CODEGRAPH_VERSION)' in deploy
assert 'CODEX_VERSION|CODEX_ACP_VERSION|CODEGRAPH_VERSION)' in deploy
assert '[[ -z ${!key:-} ]]' in deploy, 'pre-set version environment values must be preserved'
loader_start = deploy.index('VERSION_FILE=')
loader_end = deploy.index('compose() {', loader_start)
loader = deploy[loader_start:loader_end]
version_keys = ('CODEX_VERSION', 'CODEX_ACP_VERSION', 'CODEGRAPH_VERSION')
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
assert run_loader({'CODEX_VERSION': 'latest'}).returncode != 0, 'a floating version override must fail'
# deploy renders the provider config before the container starts; the mount reads the rendered file.
assert deploy.index('render_codex_config') < deploy.index('compose up')

snapshot_updater = (root / 'scripts/update-snapshots.sh').read_text()
assert 'SNAPSHOT_ROOT="$ROOT" docker compose -f "$REPO_DIR/compose.yaml" config --images agent' in snapshot_updater
# 索引用不連網、不掛 home 的 docker run，mount 對齊 bot 的 snapshot 與 .index。
assert '-v "$ROOT:/home/node/snapshots:ro,z" -v "$INDEX_ROOT:/home/node/snapshots/.index:z"' in snapshot_updater
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
assert doc['name'] == 'agent-company'
bot = doc['services']['agent']
assert bot['container_name'] == 'agent-company'
assert bot['build']['args'] == {
    'OPENAB_IMAGE': versions['OPENAB_IMAGE'],
    'CODEX_VERSION': versions['CODEX_VERSION'],
    'CODEX_ACP_VERSION': versions['CODEX_ACP_VERSION'],
    'CODEGRAPH_VERSION': versions['CODEGRAPH_VERSION'],
    'SKILLSHARE_VERSION': versions['SKILLSHARE_VERSION'],
    'PLAYWRIGHT_CLI_VERSION': versions['PLAYWRIGHT_CLI_VERSION'],
    'SKILLSHARE_LINUX_AMD64_SHA256': versions['SKILLSHARE_LINUX_AMD64_SHA256'],
    'SKILLSHARE_LINUX_ARM64_SHA256': versions['SKILLSHARE_LINUX_ARM64_SHA256'],
    'HOST_UID': '${HOST_UID:-1000}',
    'HOST_GID': '${HOST_GID:-1000}',
}, 'bot build args must equal config/versions.env'
# agent 在這個 container 裡 install 與跑 dev server；上限讓它跑爆時只殺自己。
assert bot['mem_limit'] and bot['cpus'] and bot['pids_limit']
example = (root / 'env/openab.env.example').read_text()
for key in ('COMPANY_GATEWAY_BASE_URL', 'COMPANY_GATEWAY_API_KEY'):
    assert re.search(rf'^{key}=', example, re.M), f'{key} comes from env/openab.env'
env = bot.get('environment', {})
assert not any(k.startswith(('COMPANY_GATEWAY_', 'PARSE_DOCUMENT_', 'DOCLING_', 'OPENCODE_', 'CLAUDE_')) for k in env), env
mounts = bot['volumes']
# Home is writable like OpenAB's official PVC; the read-only mounts below sit deeper and win.
assert 'home:/home/node' in mounts and 'home' in doc['volumes']
# agent-sync's inputs. ~/.codex/config.toml stays writable because skillshare writes MCP into it.
assert './runtime/codex/config.toml:/etc/codex/config.toml:ro,z' in mounts, 'mount the rendered config, not the template'
assert './agents/AGENTS.md:/etc/codex/AGENTS.bot.md:ro,z' in mounts
assert './config/skillshare/config.yaml:/home/node/.config/skillshare/config.yaml:ro,z' in mounts
assert './config/codex/catalog.json:/etc/codex/catalog.json:ro,z' in mounts
assert not any(m.split(':')[1].startswith('/home/node/.codex/') for m in mounts if ':' in m), 'agent-sync owns ~/.codex'
assert '${SNAPSHOT_ROOT:?set SNAPSHOT_ROOT in .env}:/home/node/snapshots:ro,z' in mounts
assert '${SNAPSHOT_ROOT:?set SNAPSHOT_ROOT in .env}/.index:/home/node/snapshots/.index:z' in mounts
assert not any('opencode' in m or '.claude' in m for m in mounts), mounts
assert not any('docker.sock' in m or m.startswith('/home/') or ':/home/node:' in m for m in mounts if not m.startswith('home:')), mounts
assert not bot.get('privileged')

# Chrome sidecar: pinned by digest, shares the bot's localhost, holds no secrets.
chrome = doc['services']['chrome']
assert chrome['image'] == versions['CHROME_IMAGE'] and '@sha256:' in chrome['image']
assert chrome['network_mode'] == 'service:agent', 'agent must reach Chrome at 127.0.0.1:9222'
assert '--remote-debugging-address=127.0.0.1' in chrome['entrypoint'] and '--remote-debugging-port=9222' in chrome['entrypoint']
assert not chrome.get('env_file') and not chrome.get('environment') and not chrome.get('volumes')
mcp_args = (root / 'config/skillshare/config.yaml').read_text()
assert '/home/node/snapshots/agent-config/mcp.yaml' in mcp_args and '~/.agents/skills' in mcp_args

# agent-sync: provider replaced, skillshare's MCP tables kept, rules combined.
import importlib.machinery, importlib.util
loader = importlib.machinery.SourceFileLoader('agent_sync', str(root / 'agents/bin/agent-sync'))
spec = importlib.util.spec_from_loader('agent_sync', loader)
sync = importlib.util.module_from_spec(spec); loader.exec_module(sync)
old = 'model = "a"\n[model_providers.company]\nbase_url = "https://old"\n\n[mcp_servers.codegraph]\ncommand = \'codegraph\'\n'
new = 'model = "b"\n[model_providers.company]\nbase_url = "https://new"\n'
merged = tomllib.loads(sync.merge_provider(new, old))
assert merged['model'] == 'b' and merged['model_providers']['company']['base_url'] == 'https://new'
assert merged['mcp_servers']['codegraph']['command'] == 'codegraph', 'skillshare MCP entries must survive'
assert tomllib.loads(sync.merge_provider(new, None))['model'] == 'b'
try:
    sync.merge_provider(new + '[mcp_servers.x]\ncommand = "x"\n', None)
    raise AssertionError('provider config must not carry MCP servers')
except ValueError:
    pass
rules = sync.combine_rules('# personal\n', '# bot\n')
assert rules.index('# personal') < rules.index('# bot')

# render_codex_config fills the one placeholder and refuses a bad base URL.
import tempfile
with tempfile.TemporaryDirectory() as tmp:
    tmp = pathlib.Path(tmp)
    def render(base):
        (tmp / 'openab.env').write_text(f'COMPANY_GATEWAY_BASE_URL={base}\n')
        return subprocess.run(
            ['bash', '-c', 'source "$1/scripts/lib.sh"; render_codex_config "$1/config/codex/config.toml" "$2/openab.env" "$2/out/config.toml"',
             'test', str(root), str(tmp)], capture_output=True, text=True)
    ok = render('https://gateway.example/api')
    assert ok.returncode == 0, ok.stderr
    rendered = tomllib.loads((tmp / 'out/config.toml').read_text())
    assert rendered['model_providers']['company']['base_url'] == 'https://gateway.example/api'
    for bad in ('http://gateway.example', 'https://gateway.example/', ''):
        assert render(bad).returncode != 0, f'render must refuse {bad!r}'

# 改名前的 crontab 標記在重裝時要一起拿掉，不然新舊排程同時跑。
cron_installer = (root / 'scripts/install-sync-cron.sh').read_text()
assert "'# work-agent-snapshots'" in cron_installer and "'# work-agent-artifact-cleanup'" in cron_installer
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

if ((${#LIMITED[@]})); then
  printf 'LIMITED VERIFICATION\n'
  printf ' - %s\n' "${LIMITED[@]}"
fi
printf 'Static checks passed.\n'
