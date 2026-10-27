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
test -x "$ROOT/agents/bin/parse-document"
test -x "$ROOT/agents/bin/sandbox-run"
test -x "$ROOT/sandbox/runner"
test -x "$ROOT/sandbox/sandbox-screenshot"
test -x "$ROOT/agents/bin/thread-workspace"

python3 - "$ROOT" <<'PY'
import json, os, pathlib, re, subprocess, tomllib
root = pathlib.Path(__import__('sys').argv[1])
cfg = tomllib.loads((root / 'config/openab.toml').read_text())
assert cfg['slack']['allow_all_users'] is True
assert cfg['agent']['command'] == 'opencode'
assert cfg['agent']['args'] == ['acp']
for key in ('COMPANY_GATEWAY_API_KEY', 'COMPANY_GATEWAY_BASE_URL', 'SLACK_BOT_TOKEN',
            'PARSE_DOCUMENT_ALLOWED_HOST', 'DOCLING_ARTIFACTS_PATH'):
    assert key in cfg['agent']['inherit_env']
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
for key in ('OPENCODE_VERSION', 'OMO_VERSION', 'CODEGRAPH_VERSION',
            'CLAUDE_AGENT_ACP_VERSION', 'CLAUDE_CODE_VERSION', 'DOCLING_VERSION',
            'PLAYWRIGHT_VERSION'):
    assert re.fullmatch(r'\d+\.\d+\.\d+', versions[key]), key
assert re.fullmatch(r'node:\d+\.\d+\.\d+-bookworm@sha256:[0-9a-f]{64}', versions['SANDBOX_NODE_IMAGE']), \
    'SANDBOX_NODE_IMAGE must be an exact node bookworm tag with a digest'
sandbox_dockerfile = (root / 'sandbox/Dockerfile').read_text()
assert 'ARG SANDBOX_NODE_IMAGE\nFROM ${SANDBOX_NODE_IMAGE}\n' in sandbox_dockerfile
assert 'ARG PLAYWRIGHT_VERSION\n' in sandbox_dockerfile, 'Playwright version must come from versions.env'
assert 'install --with-deps chromium' in sandbox_dockerfile
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
assert 'version_keys=(OPENCODE_VERSION OMO_VERSION CLAUDE_AGENT_ACP_VERSION CLAUDE_CODE_VERSION)' in deploy
assert 'OPENCODE_VERSION|OMO_VERSION|CLAUDE_AGENT_ACP_VERSION|CLAUDE_CODE_VERSION)' in deploy
assert '[[ -z ${!key:-} ]]' in deploy, 'pre-set version environment values must be preserved'
loader_start = deploy.index('VERSION_FILE=')
loader_end = deploy.index('compose() {', loader_start)
loader = deploy[loader_start:loader_end]
version_keys = ('OPENCODE_VERSION', 'OMO_VERSION', 'CLAUDE_AGENT_ACP_VERSION', 'CLAUDE_CODE_VERSION')
print_versions = 'printf "%s\\n" ' + ' '.join(f'"${{{key}}}"' for key in version_keys)
clean_env = {'PATH': os.environ.get('PATH', '')}
loaded = subprocess.run(
    ['bash', '-c', 'set -euo pipefail; ROOT="$1"; ' + loader + print_versions, 'test', str(root)],
    env=clean_env, check=True, capture_output=True, text=True,
).stdout.splitlines()
assert loaded == [versions[key] for key in version_keys], 'clean deploy environment must load pinned versions'
overridden = subprocess.run(
    ['bash', '-c', 'set -euo pipefail; ROOT="$1"; ' + loader + print_versions, 'test', str(root)],
    env={**clean_env, 'OPENCODE_VERSION': '9.8.7'}, check=True, capture_output=True, text=True,
).stdout.splitlines()
assert overridden == ['9.8.7', *[versions[key] for key in version_keys[1:]]], 'explicit version override must be preserved'

snapshot_updater = (root / 'scripts/update-snapshots.sh').read_text()
assert 'SNAPSHOT_ROOT="$ROOT" docker compose -f "$REPO_DIR/compose.yaml" run' in snapshot_updater
assert 'docker compose -f "$ROOT/compose.yaml" run' not in snapshot_updater

compose = (root / 'compose.yaml').read_text()
assert '${OPENAB_IMAGE' not in compose
assert '${OPENAB_AGENT_RUNTIME' not in compose
assert '${OPENAB_CONFIG' not in compose
assert 'openab.toml:/etc/openab/config.toml' in compose
assert '@sha256:' in compose
assert ':latest' not in compose and ':stable' not in compose

opencode = json.loads((root / 'config/opencode/opencode.json').read_text())
assert opencode['instructions'] == ['/home/node/CLAUDE.md']
assert opencode['skills']['paths'] == ['/home/node/.claude/skills']
skill_permission = opencode['permission']['skill']
assert skill_permission.get('*') == 'deny'
assert skill_permission.get('show-me') == 'allow'
assert './agents/CLAUDE.md:/home/node/CLAUDE.md:ro,z' in compose
assert '${SNAPSHOT_ROOT:?set SNAPSHOT_ROOT in .env}/agent-config/skills:/home/node/.claude/skills:ro,z' in compose
assert './config/opencode/opencode.json:/home/node/.config/opencode/opencode.json:ro,z' in compose
assert './config/opencode/oh-my-opencode-slim.json:/home/node/.config/opencode/oh-my-opencode-slim.json:ro,z' in compose
assert '- opencode-config:/home/node/.config/opencode' in compose
PY

grep -Fq 'allow_all_users = true' "$ROOT/config/openab.toml"
grep -Fq 'WORK_HELPER_ISSUE_MODE=manual' "$ROOT/env/openab.env.example"
grep -Fq 'PARSE_DOCUMENT_ALLOWED_HOST: 99de68928da234ebcf0c9370443ad7ee.r2.cloudflarestorage.com' "$ROOT/compose.yaml"
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

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  (cd "$ROOT" && docker compose config --quiet)
else
  limited 'docker compose config --quiet (Docker unavailable)'
fi

"$ROOT/tests/image-runtime.py"
"$ROOT/tests/slack-thread-artifact.py"
"$ROOT/tests/artifact-path-safety.py"
"$ROOT/tests/provider-route.py"
"$ROOT/tests/sandbox-runner.py"
"$ROOT/tests/sandbox-run.py"
"$ROOT/tests/thread-workspace.py"

if ((${#LIMITED[@]})); then
  printf 'LIMITED VERIFICATION\n'
  printf ' - %s\n' "${LIMITED[@]}"
fi
printf 'Static checks passed.\n'
