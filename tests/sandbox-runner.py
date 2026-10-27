#!/usr/bin/env python3
"""prototype sandbox runner 的離線契約測試。

在本機起一個真的 runner HTTP server（只綁 127.0.0.1、隨機 port），用暫存目錄
當 thread workspace、out 與 scratch。不需要 Docker、不會連外網；跑的指令都是
`sh -c` 小腳本，不是真的 pnpm／vite。
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "sandbox/runner"
KEY = "C0123456789-1710000000.123456"
OTHER_KEY = "C0123456789-1710000999.000001"
failures: list[str] = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}" if detail else name)


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_loader("sandbox_runner", importlib.machinery.SourceFileLoader("sandbox_runner", str(RUNNER)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Harness:
    def __init__(self, runner, root: Path):
        self.root = root
        self.work, self.out, self.scratch = root / "work", root / "out", root / "scratch"
        for path in (self.work, self.out, self.scratch):
            path.mkdir()
        self.server = runner.make_server(("127.0.0.1", 0), work_root=self.work, out_root=self.out, scratch_root=self.scratch, quiet=True)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown(); self.server.server_close()

    def repo(self, key=KEY, name="frontend"):
        path = self.work / key / "repos" / name
        (path / ".git").mkdir(parents=True, exist_ok=True)
        (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (path / "src").mkdir(exist_ok=True)
        (path / "src" / "app.css").write_text(".a { color: red; }\n")
        (path / "package.json").write_text('{"name": "frontend"}\n')
        return path

    def post(self, payload, path="/run"):
        body = json.dumps(payload).encode()
        request = Request(self.url + path, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


def run(h, script, key=KEY, repo="frontend", **extra):
    return h.post({"workspace": f"{key}/repos/{repo}", "argv": ["sh", "-c", script], **extra})


def check_copy_and_run(h):
    """原始碼複製到 scratch（不含 .git），在 scratch 跑，產物寫到 out/<key>/。"""
    h.repo()
    status, body = run(h, 'test ! -e .git && cat src/app.css && echo shot > "$OUT_DIR/home.png" && exit 7')
    check("run returns 200", status == 200, f"{status} {body}")
    check("command exit code is reported", body.get("exit_code") == 7, str(body))
    check("command ran in a copy without .git", ".a { color: red; }" in body.get("output", ""), body.get("output", ""))
    check("artifact lands in out/<key>/", (h.out / KEY / "home.png").read_text() == "shot\n" if (h.out / KEY / "home.png").exists() else False)
    check("files lists the artifact", body.get("files") == ["home.png"], str(body.get("files")))
    check("out_key names the thread", body.get("out_key") == KEY, str(body))


def check_no_token_reaches_job(h):
    """runner 自己 env 裡就算有 token，job 也看不到；只拿得到白名單。"""
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-a-secret"
    os.environ["COMPANY_GATEWAY_API_KEY"] = "sk-test-not-a-secret"
    os.environ["GOMEMLIMIT"] = "1GiB"
    try:
        status, body = run(h, "env")
    finally:
        del os.environ["SLACK_BOT_TOKEN"], os.environ["COMPANY_GATEWAY_API_KEY"], os.environ["GOMEMLIMIT"]
    output = body.get("output", "")
    check("env run returns 200", status == 200, str(body))
    check("job never sees SLACK_BOT_TOKEN", "xoxb-test" not in output and "SLACK_" not in output, output)
    check("job never sees gateway key", "sk-test" not in output and "COMPANY_" not in output, output)
    check("job gets OUT_DIR", f"OUT_DIR={h.out / KEY}" in output, output)
    check("esbuild memory cap reaches the job", "GOMEMLIMIT=1GiB" in output, output)
    check("dev server never tries to open a browser", "BROWSER=none" in output, output)
    check("job runs with CI=1 so repo prepare hooks skip husky", "CI=1" in output, output)


def check_background_server_is_killed(h):
    """指令結束後，背景的 dev server 也要被收掉，不能留在格子裡。"""
    pid_file = h.root / "devserver.pid"
    status, body = run(h, f"sleep 300 & echo $! > {pid_file}; echo started")
    check("background run returns 200", status == 200, str(body))
    pid = int(pid_file.read_text()) if pid_file.exists() else None
    check("background pid recorded", pid is not None)
    if pid is not None:
        alive = Path(f"/proc/{pid}").exists() and "Z" not in Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[:1]
        check("background dev server is gone after the job", not alive, f"pid {pid} still running")


def check_timeout(h):
    started = time.monotonic()
    status, body = run(h, "echo begin; sleep 300", timeout_seconds=1)
    check("timeout returns 200 with a result", status == 200, str(body))
    check("timeout is reported", body.get("timed_out") is True, str(body))
    check("timeout output kept", "begin" in body.get("output", ""), str(body))
    check("timeout kills promptly", time.monotonic() - started < 20)


def check_busy(h):
    results = {}
    def slow():
        results["slow"] = run(h, "sleep 2")
    thread = threading.Thread(target=slow); thread.start()
    time.sleep(0.7)
    status, body = run(h, "true")
    thread.join()
    check("second concurrent job gets 409", status == 409, f"{status} {body}")
    check("first job still completes", results.get("slow", (0,))[0] == 200, str(results))


def check_rejected_inputs(h):
    bad = [
        ("absolute workspace", {"workspace": f"/home/node/work/{KEY}/repos/frontend", "argv": ["true"]}),
        ("dotdot repo", {"workspace": f"{KEY}/repos/..", "argv": ["true"]}),
        ("traversal key", {"workspace": f"../{KEY}/repos/frontend", "argv": ["true"]}),
        ("bad key", {"workspace": "evil-1/repos/frontend", "argv": ["true"]}),
        ("extra segment", {"workspace": f"{KEY}/repos/frontend/src", "argv": ["true"]}),
        ("string argv", {"workspace": f"{KEY}/repos/frontend", "argv": "rm -rf /"}),
        ("empty argv", {"workspace": f"{KEY}/repos/frontend", "argv": []}),
        ("huge timeout", {"workspace": f"{KEY}/repos/frontend", "argv": ["true"], "timeout_seconds": 99999}),
    ]
    for name, payload in bad:
        status, _ = h.post(payload)
        check(f"rejects {name} with 400", status == 400, str(status))
    status, _ = h.post({"workspace": f"{KEY}/repos/missing", "argv": ["true"]})
    check("missing workspace is 404", status == 404, str(status))
    link_key = "C0123456789-1710000555.000001"
    (h.work / link_key).symlink_to(h.work / KEY)
    status, _ = h.post({"workspace": f"{link_key}/repos/frontend", "argv": ["true"]})
    check("symlinked workspace is rejected", status == 400, str(status))
    status, _ = h.post({"argv": ["true"]}, path="/exec")
    check("unknown path is 404", status == 404, str(status))


def check_scratch_reuse(h):
    """同一個 thread：保留 node_modules、反映刪改；換 thread：scratch 清空。"""
    source = h.repo()
    run(h, "mkdir -p node_modules sub/node_modules && touch node_modules/kept sub/node_modules/kept stale-build")
    (source / "src" / "app.css").write_text(".a { color: blue; }\n")
    os.utime(source / "src" / "app.css", (time.time() + 5, time.time() + 5))
    (source / "package.json").unlink()
    (source / "sub").mkdir(exist_ok=True)
    status, body = run(h, "ls node_modules sub/node_modules; cat src/app.css; test -e package.json && echo HAS_PKG; test -e stale-build && echo HAS_STALE; true")
    output = body.get("output", "")
    check("same thread keeps node_modules", output.count("kept") == 2, output)
    check("edited source is re-synced", "color: blue" in output, output)
    check("deleted source file is removed from scratch", "HAS_PKG" not in output, output)
    check("files the job created outside node_modules are dropped", "HAS_STALE" not in output, output)
    h.repo(key=OTHER_KEY)
    status, body = run(h, "test -e node_modules && echo LEAK; ls ..; true", key=OTHER_KEY)
    check("other thread starts from a clean scratch", "LEAK" not in body.get("output", ""), body.get("output", ""))
    check("other thread artifacts go to its own out dir", body.get("out_key") == OTHER_KEY, str(body))


def check_workspace_untouched(h):
    """scratch 裡怎麼改都不會回寫 thread workspace。"""
    source = h.repo()
    before = (source / "src" / "app.css").read_text()
    run(h, "echo hacked > src/app.css; mkdir -p .git/hooks && echo evil > .git/hooks/pre-commit")
    check("workspace source unchanged", (source / "src" / "app.css").read_text() == before)
    check("workspace .git/hooks not planted", not (source / ".git" / "hooks").exists())


def main():
    runner = load()
    root = Path(tempfile.mkdtemp(prefix="sandbox-runner-test-"))
    h = Harness(runner, root)
    try:
        check_copy_and_run(h)
        check_no_token_reaches_job(h)
        check_background_server_is_killed(h)
        check_timeout(h)
        check_busy(h)
        check_rejected_inputs(h)
        check_scratch_reuse(h)
        check_workspace_untouched(h)
    finally:
        h.close(); shutil.rmtree(root, ignore_errors=True)
    if failures:
        print("sandbox-runner: failed", *failures, sep="\n  - ", file=sys.stderr); return 1
    print("Sandbox runner checks passed (local runner on 127.0.0.1; no Docker, no network)."); return 0


if __name__ == "__main__":
    sys.exit(main())
