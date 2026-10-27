#!/usr/bin/env python3
"""thread-workspace 的離線契約測試：tmp 目錄裡的假 snapshot，不開 socket、不碰 Slack。

驗的是 helper 自己的承諾：參數格式、路徑由 helper 組成、第二次呼叫不重 clone、
`--shared` clone 在 workspace commit 不會動到 snapshot、symlink 不會把寫入帶出 root。
不驗 container 內的 mount、uid 或 cron 清理。
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "agents/bin/thread-workspace"
CHANNEL = "C0123456789"
THREAD_TS = "1710000000.123456"
REPO = "teamsync-app"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}
failures: list[str] = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}" if detail else name)


def load():
    # agents/bin/ 會被 COPY 進 image，不要在裡面留下 __pycache__。
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_loader("thread_workspace", importlib.machinery.SourceFileLoader("thread_workspace", str(CLI)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(cli, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try: code = cli.main(argv)
        except SystemExit as exc: code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def check_rejected_inputs(cli):
    """格式不合的參數一律 exit 2，且 work root 底下什麼都不建。"""
    cases = [
        ("empty channel", ["", THREAD_TS, REPO]),
        ("lowercase channel", ["c0123456789", THREAD_TS, REPO]),
        ("channel with slash", ["C0123/56789", THREAD_TS, REPO]),
        ("channel traversal", ["..", THREAD_TS, REPO]),
        ("empty thread_ts", [CHANNEL, "", REPO]),
        ("thread_ts traversal", [CHANNEL, "..", REPO]),
        ("thread_ts with slash", [CHANNEL, "1710000000/123456", REPO]),
        ("thread_ts without fraction", [CHANNEL, "1710000000", REPO]),
        ("empty repo", [CHANNEL, THREAD_TS, ""]),
        ("repo dot", [CHANNEL, THREAD_TS, "."]),
        ("repo traversal", [CHANNEL, THREAD_TS, ".."]),
        ("repo with slash", [CHANNEL, THREAD_TS, "teamsync-app/../x"]),
        ("repo absolute", [CHANNEL, THREAD_TS, "/etc"]),
        ("repo with newline", [CHANNEL, THREAD_TS, "teamsync-app\n"]),
        ("repo option-like", [CHANNEL, THREAD_TS, "--upload-pack=touch"]),
        ("repo not in snapshot", [CHANNEL, THREAD_TS, "no-such-repo"]),
        ("too few args", [CHANNEL, THREAD_TS]),
        ("too many args", [CHANNEL, THREAD_TS, REPO, "extra"]),
    ]
    for name, argv in cases:
        code, out, err = run(cli, argv)
        check(f"{name} exits invalid input", code == cli.EXIT_INVALID_INPUT, f"code={code} err={err!r}")
        check(f"{name} prints no path", out == "", repr(out))
    check("rejected inputs create nothing", list(cli.WORK_ROOT.iterdir()) == [], str(list(cli.WORK_ROOT.iterdir())))


def check_clone_then_reuse(cli):
    """第一次 clone 出完整 checkout；第二次回傳同一路徑，不重 clone。"""
    expected = cli.WORK_ROOT / f"{CHANNEL}-{THREAD_TS}" / "repos" / REPO
    snapshot = cli.SNAPSHOT_ROOT / REPO
    code, out, err = run(cli, [CHANNEL, THREAD_TS, REPO])
    check("first call exits 0", code == 0, err)
    check("first call prints the composed path", out == f"{expected}\n", repr(out))
    check("checkout is complete", (expected / "src/app.ts").read_text() == "export const x = 1;\n")
    check("checkout is at snapshot HEAD", git(expected, "rev-parse", "HEAD") == git(snapshot, "rev-parse", "HEAD"))
    check("worktree is clean", git(expected, "status", "--porcelain") == "")
    check("no temp clone left behind", sorted(p.name for p in expected.parent.iterdir()) == [REPO])

    # 第二次呼叫前在 workspace 留下記號；重 clone 會把它洗掉。
    (expected / "agent-edit.txt").write_text("keep me\n")
    git(snapshot, "commit", "--quiet", "--allow-empty", "-m", "snapshot moved")
    # 第二次呼叫完全不該碰 snapshot：把它暫時移走，重 clone 就會失敗。
    hidden = snapshot.with_name(f"{REPO}.hidden"); snapshot.rename(hidden)
    try: code, out, err = run(cli, [CHANNEL, THREAD_TS, REPO])
    finally: hidden.rename(snapshot)
    check("second call exits 0", code == 0, err)
    check("second call prints the same path", out == f"{expected}\n", repr(out))
    check("second call does not re-clone", (expected / "agent-edit.txt").exists())
    check("second call does not follow snapshot", git(expected, "rev-parse", "HEAD") != git(snapshot, "rev-parse", "HEAD"))


def check_shared_commit_isolation(cli):
    """clone 是 `--shared`（借 snapshot 物件），在 workspace commit 不會改到 snapshot。"""
    thread_ts = "1710000000.222222"
    snapshot = cli.SNAPSHOT_ROOT / REPO
    code, out, err = run(cli, [CHANNEL, thread_ts, REPO])
    check("shared clone exits 0", code == 0, err)
    workspace = Path(out.strip())
    alternates = workspace / ".git/objects/info/alternates"
    check("clone borrows snapshot objects", alternates.is_file()
          and Path(alternates.read_text().strip()) == snapshot / ".git/objects",
          alternates.read_text() if alternates.is_file() else "no alternates file")

    before_head = git(snapshot, "rev-parse", "HEAD")
    before_objects = sorted(str(p) for p in (snapshot / ".git/objects").rglob("*") if p.is_file())
    before_status = git(snapshot, "status", "--porcelain")
    (workspace / "src/app.ts").write_text("export const x = 2;\n")
    git(workspace, "commit", "--quiet", "-am", "agent change")
    check("workspace commit landed", git(workspace, "rev-parse", "HEAD") != before_head)
    check("snapshot HEAD unchanged", git(snapshot, "rev-parse", "HEAD") == before_head)
    check("snapshot objects unchanged",
          sorted(str(p) for p in (snapshot / ".git/objects").rglob("*") if p.is_file()) == before_objects)
    check("snapshot worktree unchanged", git(snapshot, "status", "--porcelain") == before_status
          and (snapshot / "src/app.ts").read_text() == "export const x = 1;\n")


def check_symlink_escape(cli, root):
    """路徑上任何一層被換成 symlink 都要失敗，而且 root 外面不能多出 clone。"""
    outside = root / "outside"; outside.mkdir()
    layouts = {
        "thread dir is a symlink": ("1710000000.333333", lambda key_dir: key_dir.symlink_to(outside, target_is_directory=True)),
        "repos dir is a symlink": ("1710000000.444444", lambda key_dir: (key_dir.mkdir(), (key_dir / "repos").symlink_to(outside, target_is_directory=True))),
        "repo dir is a symlink": ("1710000000.555555", lambda key_dir: ((key_dir / "repos").mkdir(parents=True), (key_dir / "repos" / REPO).symlink_to(outside, target_is_directory=True))),
    }
    for name, (thread_ts, plant) in layouts.items():
        plant(cli.WORK_ROOT / f"{CHANNEL}-{thread_ts}")
        code, out, err = run(cli, [CHANNEL, thread_ts, REPO])
        check(f"{name} is refused", code == cli.EXIT_RUNTIME, f"code={code} out={out!r} err={err!r}")
        check(f"{name} prints no path", out == "", repr(out))
        check(f"{name} writes nothing outside", list(outside.iterdir()) == [], str(list(outside.iterdir())))

    # snapshot 裡的 repo 本身是 symlink：不 clone 它指到的地方。
    (cli.SNAPSHOT_ROOT / "linked-repo").symlink_to(cli.SNAPSHOT_ROOT / REPO, target_is_directory=True)
    code, out, err = run(cli, [CHANNEL, "1710000000.666666", "linked-repo"])
    check("symlinked snapshot repo is refused", code == cli.EXIT_INVALID_INPUT, f"code={code} err={err!r}")


def make_snapshot(root: Path) -> Path:
    snapshot = root / "code"
    repo = snapshot / REPO
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "README.md").write_text("snapshot\n")
    (repo / "src").mkdir()
    (repo / "src/app.ts").write_text("export const x = 1;\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "snapshot")
    return snapshot


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV},
                          check=True, capture_output=True, text=True).stdout.strip()


def main():
    cli = load(); root = Path(tempfile.mkdtemp(prefix="thread-workspace-test-"))
    try:
        cli.SNAPSHOT_ROOT = make_snapshot(root)
        cli.WORK_ROOT = root / "work"; cli.WORK_ROOT.mkdir()
        check_rejected_inputs(cli)
        check_clone_then_reuse(cli)
        check_shared_commit_isolation(cli)
        check_symlink_escape(cli, root)
    finally: shutil.rmtree(root, ignore_errors=True)
    if failures:
        print("thread-workspace: failed", *failures, sep="\n  - ", file=sys.stderr); return 1
    print("thread-workspace checks passed (local tmp snapshot; no network)."); return 0


if __name__ == "__main__": sys.exit(main())
