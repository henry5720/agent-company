#!/usr/bin/env python3
"""bot 端 `sandbox-run` 的離線契約測試；用假的 urlopen，不會開 socket。"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "agents/bin/sandbox-run"
KEY = "C0123456789-1710000000.123456"
REPO = f"/home/node/work/{KEY}/repos/teamsync-frontend"
URLS = "http://slot-a:8080 http://slot-b:8080"
failures: list[str] = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}" if detail else name)


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_loader("sandbox_run", importlib.machinery.SourceFileLoader("sandbox_run", str(CLI)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, payload): self.payload = json.dumps(payload).encode()
    def read(self): return self.payload
    def __enter__(self): return self
    def __exit__(self, *_): return False


class Runner:
    """假 runner 群。`plan` 依 URL 決定回什麼：dict＝200、int＝HTTP 錯誤、'down'＝連不上。"""

    def __init__(self, plan):
        self.plan, self.requests = plan, []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        base = request.full_url.rsplit("/", 1)[0]
        reply = self.plan.get(base, "down")
        if reply == "down":
            raise URLError("connection refused")
        if isinstance(reply, int):
            raise HTTPError(request.full_url, reply, "err", {}, io.BytesIO(json.dumps({"error": f"mock {reply}"}).encode()))
        return Response(reply)


def result(**extra):
    base = {"exit_code": 0, "timed_out": False, "duration_seconds": 1.0, "output": "vite ready\n", "out_key": KEY, "files": ["home.png"]}
    base.update(extra)
    return base


def run(cli, argv, plan):
    fake = Runner(plan); cli.urlopen = fake
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try: code = cli.main(argv)
        except SystemExit as exc: code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue(), fake.requests


def check_happy_path(cli):
    plan = {"http://slot-a:8080": result(), "http://slot-b:8080": result()}
    code, out, err, requests = run(cli, [REPO, "--", "pnpm", "install"], plan)
    check("happy path exits with the command code", code == 0, err)
    check("one request on success", len(requests) == 1, str(len(requests)))
    request, timeout = requests[0]
    body = json.loads(request.data)
    check("posts to /run", request.full_url.endswith("/run") and request.get_method() == "POST", request.full_url)
    check("sends JSON", request.headers.get("Content-type") == "application/json", str(request.headers))
    check("maps bot path to runner workspace", body.get("workspace") == f"{KEY}/repos/teamsync-frontend", str(body))
    check("sends argv after -- verbatim", body.get("argv") == ["pnpm", "install"], str(body))
    check("never sends tokens", "xoxb" not in request.data.decode() and "authorization" not in {k.lower() for k in request.headers}, str(request.headers))
    check("http timeout exceeds the job timeout", timeout is not None and timeout > body.get("timeout_seconds", 900), str(timeout))
    check("prints the job output", "vite ready" in out, out)
    check("points at the bot-side out dir", f"/home/node/out/{KEY}/home.png" in err, err)


def check_exit_codes(cli):
    plan = {"http://slot-a:8080": result(exit_code=7), "http://slot-b:8080": result(exit_code=7)}
    code, _, _, _ = run(cli, [REPO, "--", "false"], plan)
    check("propagates command exit code", code == 7, str(code))
    plan = {"http://slot-a:8080": result(exit_code=-9, timed_out=True), "http://slot-b:8080": result(exit_code=-9, timed_out=True)}
    code, _, err, _ = run(cli, ["--timeout", "60", REPO, "--", "pnpm", "dev"], plan)
    check("timeout exits 124", code == 124, str(code))
    check("timeout is explained", "timed out" in err, err)


def check_slot_selection(cli):
    plan = {"http://slot-a:8080": result(), "http://slot-b:8080": result()}
    _, _, _, first = run(cli, [REPO, "--", "true"], plan)
    _, _, _, again = run(cli, [REPO, "--", "true"], plan)
    check("same thread prefers the same slot", first[0][0].full_url == again[0][0].full_url)
    busy_first = {first[0][0].full_url.rsplit("/", 1)[0]: 409}
    other = next(url for url in URLS.split() if url not in busy_first)
    code, _, err, requests = run(cli, [REPO, "--", "true"], {**busy_first, other: result()})
    check("busy slot falls over to the other", code == 0 and len(requests) == 2, err)
    code, _, err, requests = run(cli, [REPO, "--", "true"], {"http://slot-a:8080": 409, "http://slot-b:8080": "down"})
    check("all slots busy or down exits 3", code == 3, str(code))
    check("all busy is explained", "busy" in err, err)


def check_runner_rejection(cli):
    code, _, err, requests = run(cli, [REPO, "--", "true"], {"http://slot-a:8080": 404, "http://slot-b:8080": 404})
    check("runner 404 is invalid input", code == 2, str(code))
    check("runner 404 does not retry the other slot", len(requests) == 1, str(len(requests)))
    check("runner message is shown", "mock 404" in err, err)


def check_rejected_inputs(cli):
    bad = [
        ("no separator", [REPO, "pnpm", "install"]),
        ("no command", [REPO, "--"]),
        ("snapshot path", ["/home/node/code/teamsync-frontend", "--", "true"]),
        ("relative path", [f"work/{KEY}/repos/x", "--", "true"]),
        ("traversal", [f"/home/node/work/{KEY}/repos/../../../code/x", "--", "true"]),
        ("subdir", [f"{REPO}/frontend", "--", "true"]),
        ("bad key", ["/home/node/work/evil/repos/x", "--", "true"]),
        ("timeout too long", ["--timeout", "99999", REPO, "--", "true"]),
        ("no url option", ["--url", "http://evil", REPO, "--", "true"]),
    ]
    for name, argv in bad:
        code, _, _, requests = run(cli, argv, {})
        check(f"rejects {name}", code == 2, str(code))
        check(f"rejects {name} before network", not requests)
    code, _, _, requests = run(cli, [REPO + "/", "--", "true"], {"http://slot-a:8080": result(), "http://slot-b:8080": result()})
    check("trailing slash is accepted", code == 0 and json.loads(requests[0][0].data)["workspace"] == f"{KEY}/repos/teamsync-frontend")


def main():
    os.environ["SANDBOX_RUNNER_URLS"] = URLS
    cli = load()
    check_happy_path(cli)
    check_exit_codes(cli)
    check_slot_selection(cli)
    check_runner_rejection(cli)
    check_rejected_inputs(cli)
    if failures:
        print("sandbox-run: failed", *failures, sep="\n  - ", file=sys.stderr); return 1
    print("sandbox-run checks passed (mocked runner; no real call was made)."); return 0


if __name__ == "__main__":
    sys.exit(main())
