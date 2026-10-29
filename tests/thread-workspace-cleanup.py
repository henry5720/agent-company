#!/usr/bin/env python3
"""`thread-workspace cleanup` 的離線測試（spec #21 §5；#33）。

tmp 目錄當 WORK_ROOT，Slack 換成 127.0.0.1 上的假 server，不打真的 Slack。
驗的是子命令的承諾：最後修改超過 7 天的刪掉；總量超過上限時從最久沒動的刪起；
被刪的 thread 收到「工作副本已清除…」通知；symlink 不跟；通知失敗仍刪、但 exit 非 0；
最後一行印出清完後的總量給 host 告警用。不驗 cron 真的觸發、也不驗真的 Slack。
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "agents/bin/thread-workspace"
DAY = 86400
NOTICE = "工作副本已清除，再改會從最新 snapshot 重來"
failures: list[str] = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}" if detail else name)


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_loader("thread_workspace", importlib.machinery.SourceFileLoader("thread_workspace", str(CLI)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(cli, argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    saved = dict(os.environ)
    os.environ.update(env or {})
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try: code = cli.main(argv)
            except SystemExit as exc: code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.environ.clear(); os.environ.update(saved)
    return code, out.getvalue(), err.getvalue()


class FakeSlack:
    """只收 chat.postMessage；`fail_channels` 裡的 channel 回 ok:false。"""

    def __init__(self):
        self.posts: list[dict] = []
        self.fail_channels: set[str] = set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
                fields = {k: v[0] for k, v in parse_qs(body).items()}
                fake.posts.append({"path": self.path, "auth": self.headers.get("Authorization"), **fields})
                ok = fields.get("channel") not in fake.fail_channels
                payload = json.dumps({"ok": ok, **({} if ok else {"error": "channel_not_found"})}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def close(self):
        self.server.shutdown()


def make_thread(root: Path, key: str, age_days: float, size: int = 1000, fresh_file_days: float | None = None) -> Path:
    """建一個 `<key>/repos/app/` workspace，所有東西的 mtime 設成 age_days 前。"""
    base = root / key
    repo = base / "repos" / "app"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "blob.bin").write_bytes(b"x" * size)
    (repo / "README.md").write_text("hi\n")
    stamp = time.time() - age_days * DAY
    for path in sorted(base.rglob("*"), key=lambda p: len(p.parts), reverse=True) + [base]:
        os.utime(path, (stamp, stamp), follow_symlinks=False)
    if fresh_file_days is not None:
        recent = time.time() - fresh_file_days * DAY
        os.utime(repo / "src" / "blob.bin", (recent, recent))
    return base


def reset(root: Path):
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir(): entry.unlink()
        else: shutil.rmtree(entry)


def posts_for(slack, key):
    channel, thread_ts = key.split("-", 1)
    return [p for p in slack.posts if p.get("channel") == channel and p.get("thread_ts") == thread_ts]


def check_age(cli, slack):
    work = cli.WORK_ROOT
    old = make_thread(work, "C0123456789-1710000000.000001", age_days=8)
    deep_recent = make_thread(work, "C0123456789-1710000000.000002", age_days=9, fresh_file_days=1)
    young = make_thread(work, "D0123456789-1710000000.000003", age_days=6)
    code, out, err = run(cli, ["cleanup"], {"SLACK_BOT_TOKEN": "xoxb-test"})
    check("age cleanup exits 0", code == 0, f"code={code} err={err!r}")
    check("8-day-old thread removed", not old.exists())
    check("thread with a file edited yesterday kept (newest mtime in the tree counts)", deep_recent.exists())
    check("6-day-old thread kept", young.exists())
    posts = posts_for(slack, old.name)
    check("removed thread notified once", len(posts) == 1, str(slack.posts))
    if posts:
        p = posts[0]
        check("notice uses chat.postMessage", p["path"] == "/api/chat.postMessage", p["path"])
        check("notice carries the bot token", p["auth"] == "Bearer xoxb-test", str(p["auth"]))
        check("notice text is the spec wording", p.get("text") == NOTICE, p.get("text"))
    check("kept threads not notified", not posts_for(slack, deep_recent.name) and not posts_for(slack, young.name))
    last = out.strip().splitlines()[-1] if out.strip() else ""
    check("last line reports remaining bytes", last.startswith("remaining_bytes=") and int(last.split("=")[1]) > 0, out)
    reset(work); slack.posts.clear()


def check_size(cli, slack):
    work = cli.WORK_ROOT
    one_mb = 1024 * 1024
    oldest = make_thread(work, "C0123456789-1710000000.000011", age_days=5, size=one_mb)
    middle = make_thread(work, "C0123456789-1710000000.000012", age_days=3, size=one_mb)
    newest = make_thread(work, "C0123456789-1710000000.000013", age_days=1, size=one_mb)
    cli.MAX_TOTAL_BYTES = int(2.5 * one_mb)
    try:
        code, out, err = run(cli, ["cleanup"], {"SLACK_BOT_TOKEN": "xoxb-test"})
    finally:
        cli.MAX_TOTAL_BYTES = 20 * 1024 ** 3
    check("size cleanup exits 0", code == 0, f"code={code} err={err!r}")
    check("oldest thread evicted first", not oldest.exists())
    check("eviction stops once under the limit", middle.exists() and newest.exists())
    check("evicted thread notified", len(posts_for(slack, oldest.name)) == 1, str(slack.posts))
    check("surviving threads not notified", not posts_for(slack, middle.name) and not posts_for(slack, newest.name))
    remaining = int(out.strip().splitlines()[-1].split("=")[1])
    check("remaining total is under the limit", remaining <= int(2.5 * one_mb), str(remaining))
    reset(work); slack.posts.clear()


def check_symlinks_and_noise(cli, slack, root):
    work = cli.WORK_ROOT
    outside = root / "outside"; outside.mkdir()
    (outside / "precious.txt").write_text("do not delete\n")
    old_stamp = time.time() - 30 * DAY
    os.utime(outside / "precious.txt", (old_stamp, old_stamp))
    # thread key 本身是指向外面的 symlink：不跟、不刪外面的東西。
    (work / "C0123456789-1710000000.000021").symlink_to(outside, target_is_directory=True)
    # 舊 thread 裡面有指向外面的 symlink：整個 thread 刪掉，但外面的檔案還在。
    inner = make_thread(work, "C0123456789-1710000000.000022", age_days=10)
    (inner / "repos" / "app" / "escape").symlink_to(outside, target_is_directory=True)
    os.utime(inner / "repos" / "app" / "escape", (old_stamp, old_stamp), follow_symlinks=False)
    os.utime(inner / "repos" / "app", (old_stamp, old_stamp))
    # 不是 thread key 的東西不碰。
    (work / "lost+found").mkdir()
    os.utime(work / "lost+found", (old_stamp, old_stamp))
    code, out, err = run(cli, ["cleanup"], {"SLACK_BOT_TOKEN": "xoxb-test"})
    check("symlink layout exits 0", code == 0, f"code={code} err={err!r}")
    check("files behind symlinks survive", (outside / "precious.txt").exists())
    check("old thread with an inner symlink removed", not inner.exists())
    check("non-key directory left alone", (work / "lost+found").is_dir())
    check("symlinked key not notified", not posts_for(slack, "C0123456789-1710000000.000021"), str(slack.posts))
    reset(work); slack.posts.clear()
    shutil.rmtree(outside)


def check_notify_failure(cli, slack):
    work = cli.WORK_ROOT
    gone = make_thread(work, "C0999999999-1710000000.000031", age_days=8)
    slack.fail_channels.add("C0999999999")
    try:
        code, out, err = run(cli, ["cleanup"], {"SLACK_BOT_TOKEN": "xoxb-test"})
    finally:
        slack.fail_channels.clear()
    check("still deleted when Slack refuses the notice", not gone.exists())
    check("failed notice makes the run exit non-zero", code == cli.EXIT_RUNTIME, f"code={code}")
    check("failure names the thread", gone.name in err, err)
    check("failure does not leak the token", "xoxb-test" not in err + out)

    gone = make_thread(work, "C0123456789-1710000000.000032", age_days=8)
    code, out, err = run(cli, ["cleanup"], {"SLACK_BOT_TOKEN": ""})
    check("missing token still deletes", not gone.exists())
    check("missing token exits non-zero", code == cli.EXIT_RUNTIME, f"code={code} err={err!r}")
    reset(work); slack.posts.clear()


def check_os_error_does_not_abort(cli):
    """一個目錄刪不掉（OSError）不能讓整輪中止：其他照清、仍印 remaining_bytes、exit 非 0。"""
    if os.geteuid() == 0:
        print("thread-workspace-cleanup.py: running as root; OSError case skipped")
        return
    work = cli.WORK_ROOT
    stuck = work / ".cleanup-C0123456789-1710000000.000041-123"
    locked = stuck / "repos" / "app" / "locked"
    locked.mkdir(parents=True); (locked / "file").write_text("x")
    locked.chmod(0o500)
    old = make_thread(work, "C0123456789-1710000000.000042", age_days=8)
    try:
        code, out, err = run(cli, ["cleanup"], {"SLACK_BOT_TOKEN": "xoxb-test"})
    finally:
        locked.chmod(0o700)
    check("OSError on one entry exits non-zero", code == cli.EXIT_RUNTIME, f"code={code} err={err!r}")
    check("OSError names the entry", stuck.name in err, err)
    check("other threads still cleaned after an OSError", not old.exists())
    last = out.strip().splitlines()[-1] if out.strip() else ""
    check("remaining_bytes still printed after an OSError", last.startswith("remaining_bytes="), out)
    reset(work)


def check_usage(cli):
    code, _, _ = run(cli, ["cleanup", "extra"])
    check("cleanup takes no arguments", code == cli.EXIT_INVALID_INPUT, f"code={code}")


def main():
    cli = load(); root = Path(tempfile.mkdtemp(prefix="thread-workspace-cleanup-test-"))
    slack = FakeSlack()
    try:
        cli.WORK_ROOT = root / "work"; cli.WORK_ROOT.mkdir()
        cli.SLACK_API = slack.url
        check_usage(cli)
        check_age(cli, slack)
        check_size(cli, slack)
        check_symlinks_and_noise(cli, slack, root)
        check_notify_failure(cli, slack)
        check_os_error_does_not_abort(cli)
    finally:
        slack.close(); shutil.rmtree(root, ignore_errors=True)
    if failures:
        print("thread-workspace-cleanup: failed", *failures, sep="\n  - ", file=sys.stderr); return 1
    print("thread-workspace cleanup checks passed (tmp work root; fake Slack on 127.0.0.1)."); return 0


if __name__ == "__main__": sys.exit(main())
