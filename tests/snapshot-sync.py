#!/usr/bin/env python3
"""Exercise snapshot sync against local Git remotes and a fake Docker CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as directory:
    tmp = Path(directory)
    env = dict(os.environ, HOME=str(tmp), GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1')
    def git(*args):
        return subprocess.run(['git', *map(str, args)], env=env, check=True, capture_output=True, text=True)
    source = tmp / 'source'
    git('init', '-b', 'main', source)
    (source / '.codegraph').mkdir()
    original = '*.db\n'
    (source / '.codegraph/.gitignore').write_text(original)
    (source / 'main.py').write_text('def example():\n    return 42\n')
    git('-C', source, 'add', '.')
    git('-C', source, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'fixture')
    config = tmp / 'repos.conf'
    config.write_text(f'fixture|{source}|main\n')
    key = tmp / 'key'
    key.touch()
    bindir = tmp / 'bin'
    bindir.mkdir()
    docker = bindir / 'docker'
    docker.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
if a[0] == 'compose':
    if 'config' in a:
        if '--images' in a:
            print('fixture:agent\\nfixture:pause')
        else:
            print(json.dumps({'services': {'agent': {'image': 'fixture:agent'}}}))
    sys.exit(0)
if a[:2] == ['image', 'inspect']:
    sys.exit(0)
if a[0] == 'run':
    assert a[a.index('--entrypoint') + 2] == 'fixture:agent', a
    assert 'CODEGRAPH_DIR=.codegraph-company' in a, a
    assert a[-1].startswith('set -e;'), a[-1]
    snapshot = Path(os.environ['SNAPSHOT_ROOT'])
    data = snapshot / 'fixture/.codegraph-company'
    assert data.is_symlink() and data.resolve() == snapshot / '.index/fixture'
    if os.environ.get('MOCK_INDEX_FAIL'):
        sys.exit(17)
    (data / 'codegraph.db').write_bytes(b'fixture index')
    sys.exit(0)
raise AssertionError(a)
''')
    docker.chmod(0o755)
    env.update(PATH=f'{bindir}:{env["PATH"]}', SNAPSHOT_ROOT=str(tmp / 'snapshots'),
               REPOS_CONFIG=str(config), GITHUB_SSH_KEY=str(key),
               SNAPSHOT_LOG=str(tmp / 'sync.log'), XDG_STATE_HOME=str(tmp / 'state'))
    def sync():
        return subprocess.run(['bash', str(root / 'scripts/update-snapshots.sh')], env=env,
                              capture_output=True, text=True)
    for _ in range(2):
        result = sync()
        log = (tmp / 'sync.log').read_text()
        assert result.returncode == 0, log
        snapshot = tmp / 'snapshots/fixture'
        assert (snapshot / '.codegraph/.gitignore').read_text() == original
        assert not git('-C', snapshot, 'status', '--porcelain').stdout.strip(), log
        assert (tmp / 'snapshots/.index/fixture/codegraph.db').stat().st_size > 0, log
    env['MOCK_INDEX_FAIL'] = '1'
    assert sync().returncode != 0, 'index failure must fail snapshot synchronization'
print('Snapshot sync checks passed (local Git, mocked Docker).')
