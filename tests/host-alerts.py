#!/usr/bin/env python3
"""host 告警（spec #21 §8；#24 決定 4；#33）的離線測試。

跑真的 scripts/alerts.py：Caddy access log 是 tmp 目錄裡照真實格式產生的 JSON 行（含一個
gzip 輪替檔），`docker inspect` 是 tests/fake-docker，webhook 是 127.0.0.1 上的假 server。
根磁碟用量是唯一在 process 內替換的東西（`alerts.disk_usage`），因為測試不能真的塞滿磁碟。

驗四類告警的判斷與去重、`[work-agent]` 前綴、webhook 檔權限、送不出去時下一輪重送。
證明不了：真的 Slack webhook 收得到、真的 Caddy log 權限、cron 真的觸發（#35 人工驗）。
"""
from __future__ import annotations

import contextlib
import gzip
import io
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALERTS = ROOT / "scripts/alerts.py"
FAKE_DOCKER = ROOT / "tests/fake-docker"
HOUR, DAY = 3600, 86400
GIB = 1024 ** 3
# 固定的「現在」：2026-10-29 10:20 UTC。上一個完整小時是 09:00–10:00。
NOW = 1793268000 + 20 * 60
LAST_HOUR = NOW - NOW % HOUR - HOUR
Usage = namedtuple("Usage", "total used free")
failures: list[str] = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}" if detail else name)


def load(path=ALERTS, name="alerts"):
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_values_shared_with_the_clis(cli):
    """告警跟 bot 端 CLI 共用的值：分在兩個 runtime（host 與 container），各寫一份、這裡比對。"""
    workspace = load(ROOT / "agents/bin/thread-workspace", "thread_workspace")
    image = load(ROOT / "agents/bin/company-image", "company_image")
    check("thread workspace alert limit equals thread-workspace MAX_TOTAL_BYTES",
          cli.THREAD_WORKSPACE_LIMIT_BYTES == workspace.MAX_TOTAL_BYTES,
          f"{cli.THREAD_WORKSPACE_LIMIT_BYTES} != {workspace.MAX_TOTAL_BYTES}")
    check("image alert counts the User-Agent company-image sends",
          cli.IMAGE_USER_AGENT == getattr(image, "USER_AGENT", None),
          f"{cli.IMAGE_USER_AGENT!r} != {getattr(image, 'USER_AGENT', None)!r}")


class FakeWebhook:
    def __init__(self):
        self.posts: list[dict] = []
        self.status = 200
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fake.posts.append({"path": self.path, "type": self.headers.get("Content-Type"), "body": json.loads(body)})
                self.send_response(fake.status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/services/T000/B000/fake"

    def texts(self):
        return [p["body"]["text"] for p in self.posts]

    def close(self):
        self.server.shutdown()


def access_line(ts: float, image: bool = False, uri: str = "/responses", method: str = "POST", status: int = 200) -> str:
    """真的 Caddy 2.11 JSON access log 一行的形狀（#33 在本機用 work-agent-caddy 錄過）。"""
    ua = "work-agent-company-image" if image else "codex_cli_rs/0.159.0"
    return json.dumps({
        "level": "info", "ts": ts, "logger": "http.log.access.log0", "msg": "handled request",
        "request": {"remote_ip": "10.231.0.10", "remote_port": "51922", "client_ip": "10.231.0.10",
                    "proto": "HTTP/1.1", "method": method, "host": "10.231.0.1:18080", "uri": uri,
                    "headers": {"User-Agent": [ua], "Content-Type": ["application/json"],
                                "Authorization": ["REDACTED"]}},
        "bytes_read": 512, "user_id": "", "duration": 1.2, "size": 2048, "status": status,
        "resp_headers": {"Content-Type": ["text/event-stream"]},
    })


class Env:
    def __init__(self, cli, tmp: Path, webhook: FakeWebhook):
        self.cli, self.tmp, self.webhook = cli, tmp, webhook
        self.state = tmp / "state"; self.state.mkdir()
        self.logs = tmp / "proxy-logs"; self.logs.mkdir()
        self.bin = tmp / "bin"; self.bin.mkdir()
        os.symlink(FAKE_DOCKER, self.bin / "docker")
        self.inspect = tmp / "inspect.json"
        self.hook_file = tmp / "alert-webhook"
        self.hook_file.write_text(webhook.url + "\n"); self.hook_file.chmod(0o600)
        # cgroup v2 fixture：/proc/<pid>/cgroup 指到 <cgroup root>/system.slice/docker-<id>.scope，
        # 跟 deployment host（systemd cgroup driver）實際的形狀一樣。
        self.proc = tmp / "proc"; self.cgroup = tmp / "cgroup"
        cli.PROC_ROOT, cli.CGROUP_ROOT = self.proc, self.cgroup
        self.healthy()
        self.disk = Usage(100 * GIB, 50 * GIB, 50 * GIB)
        cli.disk_usage = lambda path: self.disk

    def healthy(self):
        containers = {}
        for pid, name in enumerate(("work-agent", "work-agent-sandbox-1", "work-agent-sandbox-2"), start=4100):
            containers[name] = {"Id": f"id-{name}", "Name": f"/{name}", "RestartCount": 0,
                                "State": {"Running": True, "OOMKilled": False, "Pid": pid,
                                          "StartedAt": "2026-10-01T00:00:00Z", "FinishedAt": "0001-01-01T00:00:00Z"}}
        self.containers = containers
        shutil.rmtree(self.proc, ignore_errors=True); shutil.rmtree(self.cgroup, ignore_errors=True)
        for name in containers:
            self.oom_kills(name, 0)
        self.write_inspect()
        self.size_report("thread-workspace-bytes", 5 * GIB)
        self.size_report("codex-home-bytes", GIB // 2)

    def write_inspect(self):
        self.inspect.write_text(json.dumps(self.containers))

    def oom_kills(self, name, count):
        """寫 container 的 cgroup memory.events（照真實檔案的欄位）與 /proc/<pid>/cgroup。"""
        item = self.containers[name]
        scope = f"/system.slice/docker-{item['Id']}.scope"
        proc = self.proc / str(item["State"]["Pid"]); proc.mkdir(parents=True, exist_ok=True)
        (proc / "cgroup").write_text(f"0::{scope}\n")
        events = self.cgroup / scope.lstrip("/"); events.mkdir(parents=True, exist_ok=True)
        (events / "memory.events").write_text(
            f"low 0\nhigh 0\nmax {count * 3}\noom {count}\noom_kill {count}\noom_group_kill 0\nsock_throttled 0\n")

    def size_report(self, name, value, at=None):
        (self.state / name).write_text(f"{int(at if at is not None else NOW - 20 * 60)} {value}\n")

    def write_log(self, lines: list[str], rolled: list[str] | None = None):
        for path in self.logs.iterdir(): path.unlink()
        (self.logs / "access.log").write_text("".join(l + "\n" for l in lines))
        if rolled:
            with gzip.open(self.logs / "access-2026-10-20T00-00-00.000.log.gz", "wt") as f:
                f.write("".join(l + "\n" for l in rolled))

    def run(self, now=NOW):
        saved = dict(os.environ)
        os.environ.update({"PATH": f"{self.bin}:{saved['PATH']}",
                           "FAKE_DOCKER_LOG": str(self.tmp / "docker.log"),
                           "FAKE_DOCKER_INSPECT": str(self.inspect)})
        before = len(self.webhook.posts)
        self.output = io.StringIO()
        try:
            with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
                code = self.cli.main(["--state-dir", str(self.state), "--log-dir", str(self.logs),
                                      "--webhook-file", str(self.hook_file), "--now", str(now)])
        finally:
            os.environ.clear(); os.environ.update(saved)
        return code, self.webhook.texts()[before:]


def steady_history(days: int, per_active_hour: int = 10, start_hour_utc: int = 1, active_hours: int = 9):
    """過去 `days` 天，每天 UTC start..start+active 這幾小時每小時 per_active_hour 筆，其餘 0。"""
    lines = []
    day0 = LAST_HOUR - LAST_HOUR % DAY
    for d in range(days, 0, -1):
        for h in range(start_hour_utc, start_hour_utc + active_hours):
            base = day0 - d * DAY + h * HOUR
            lines += [access_line(base + 60 * i) for i in range(per_active_hour)]
    return lines


def in_last_hour(n, image=False):
    return [access_line(LAST_HOUR + 30 * i + 1, image=image) for i in range(n)]


def reset(env: Env):
    for path in env.state.iterdir():
        if path.name not in ("thread-workspace-bytes", "codex-home-bytes"):
            path.unlink()
    env.healthy()
    env.disk = Usage(100 * GIB, 50 * GIB, 50 * GIB)


def check_quiet_run(env: Env):
    env.write_log(steady_history(8))
    code, texts = env.run()
    check("healthy host exits 0", code == 0)
    check("healthy host posts nothing", texts == [], str(texts))
    reset(env)


def check_responses_rate(env: Env):
    # 前 7 天只記錄：只有 3 天歷史時，再高也不告警。
    env.write_log(steady_history(3) + in_last_hour(500))
    code, texts = env.run()
    check("warm-up: no rate alert with under 7 days of history", not any("/responses" in t for t in texts), str(texts))
    reset(env)

    # 歷史：活躍小時中位數 10（夜裡 0 不算），門檻 5 倍 = 50；51 告警、50 不告警。
    history = steady_history(8)
    env.write_log(history + in_last_hour(50))
    code, texts = env.run()
    check("exactly 5x the median does not alert", not any("/responses" in t for t in texts), str(texts))
    reset(env)
    env.write_log(history[:len(history) // 2], rolled=history[len(history) // 2:])
    with open(env.logs / "access.log", "a") as f:
        f.write("".join(l + "\n" for l in in_last_hour(51)))
        f.write("not json\n")
        f.write("".join(access_line(LAST_HOUR + 5, uri="/models", method="GET", status=403) + "\n" for _ in range(30)))
    code, texts = env.run()
    rate = [t for t in texts if "/responses" in t]
    check("over 5x the median alerts (rolled gzip log read, junk lines and other paths ignored)", len(rate) == 1, str(texts))
    check("rate alert names count and threshold", rate and "51" in rate[0] and "10" in rate[0], str(rate))
    code, texts = env.run(now=NOW + 60)
    check("same hour is not alerted twice", not any("/responses" in t for t in texts), str(texts))
    reset(env)


def check_image_count(env: Env):
    today = NOW - NOW % DAY
    images = [access_line(today + 60 * i, image=True) for i in range(50)]
    env.write_log(steady_history(8) + images)
    code, texts = env.run()
    check("50 image requests in a day does not alert", not any("生圖" in t for t in texts), str(texts))
    env.write_log(steady_history(8) + images + [access_line(today + 9000, image=True)]
                  + [access_line(today - 100 - i, image=True) for i in range(80)])
    code, texts = env.run()
    image = [t for t in texts if "生圖" in t]
    check("51 image requests today alerts (yesterday's do not count)", len(image) == 1 and "51" in image[0], str(texts))
    code, texts = env.run(now=NOW + HOUR)
    check("image alert fires once per day", not any("生圖" in t for t in texts), str(texts))
    reset(env)


def check_containers(env: Env):
    env.write_log(steady_history(8))
    env.containers["work-agent"]["RestartCount"] = 3
    env.write_inspect()
    code, texts = env.run()
    check("first run only records RestartCount", not any("work-agent " in t or "RestartCount" in t for t in texts), str(texts))
    env.containers["work-agent"]["RestartCount"] = 4
    env.containers["work-agent-sandbox-1"]["State"].update({"OOMKilled": True, "FinishedAt": "2026-10-29T09:10:00Z"})
    env.write_inspect()
    code, texts = env.run(now=NOW + HOUR)
    joined = "\n".join(texts)
    check("RestartCount increase alerts", "work-agent" in joined and "3 → 4" in joined, joined)
    check("OOMKilled alerts", "work-agent-sandbox-1" in joined and "OOMKilled" in joined, joined)
    code, texts = env.run(now=NOW + 2 * HOUR)
    check("same OOM kill and restart are not re-alerted", texts == [], str(texts))
    # container 重建（Id 換了）：RestartCount 歸零不算增加。
    env.containers["work-agent"].update({"Id": "id-new", "RestartCount": 0})
    env.write_inspect()
    code, texts = env.run(now=NOW + 3 * HOUR)
    check("recreated container is a new baseline", not any("RestartCount" in t for t in texts), str(texts))
    del env.containers["work-agent-sandbox-2"]
    env.write_inspect()
    code, texts = env.run(now=NOW + 4 * HOUR)
    check("missing container alerts", any("work-agent-sandbox-2" in t for t in texts), str(texts))
    reset(env)


def check_child_oom(env: Env):
    """§8 ①：runner 底下的子 process（例如 esbuild，#26）被 OOM kill 時 container 不會結束，
    `State.OOMKilled`／FinishedAt 看不出來；要看 cgroup memory.events 的 oom_kill 有沒有增加。"""
    env.write_log(steady_history(8))
    code, texts = env.run()
    check("zero oom_kill does not alert", not any("OOM" in t for t in texts), str(texts))
    env.oom_kills("work-agent-sandbox-1", 1)
    code, texts = env.run(now=NOW + HOUR)
    joined = "\n".join(texts)
    check("child OOM kill (oom_kill 0 → 1) alerts", "work-agent-sandbox-1" in joined and "oom_kill" in joined, joined)
    code, texts = env.run(now=NOW + 2 * HOUR)
    check("same oom_kill count is not re-alerted", not any("OOM" in t or "oom_kill" in t for t in texts), str(texts))
    env.oom_kills("work-agent-sandbox-1", 3)
    code, texts = env.run(now=NOW + 3 * HOUR)
    joined = "\n".join(texts)
    check("another OOM in the same container lifetime alerts again", "1 → 3" in joined, joined)

    # 主 process 被 OOM kill 並留下 OOMKilled，同一輪 oom_kill 也增加：只發一條。
    env.containers["work-agent-sandbox-1"]["State"].update({"OOMKilled": True, "FinishedAt": "2026-10-29T12:00:00Z"})
    env.write_inspect()
    env.oom_kills("work-agent-sandbox-1", 4)
    code, texts = env.run(now=NOW + 4 * HOUR)
    lines = [l for t in texts for l in t.splitlines() if "work-agent-sandbox-1" in l]
    check("one OOM event in one run is one alert line", len(lines) == 1, str(lines))

    # container 重新啟動（StartedAt 換了、cgroup 計數從 0 開始）：新的 1 次也要告警。
    env.containers["work-agent-sandbox-1"]["State"].update({"OOMKilled": False, "StartedAt": "2026-10-29T15:00:00Z"})
    env.write_inspect()
    env.oom_kills("work-agent-sandbox-1", 1)
    code, texts = env.run(now=NOW + 5 * HOUR)
    joined = "\n".join(texts)
    check("OOM after a restart is compared with the new lifetime's zero", "0 → 1" in joined, joined)

    # 讀不到 memory.events：OOM 偵測沒在跑，本身就是告警。
    (env.cgroup / "system.slice/docker-id-work-agent.scope/memory.events").unlink()
    code, texts = env.run(now=NOW + 6 * HOUR)
    check("unreadable memory.events alerts", any("memory.events" in t and "`work-agent`" in t for t in texts), str(texts))
    reset(env)


def check_sizes(env: Env):
    env.write_log(steady_history(8))
    env.disk = Usage(100 * GIB, 81 * GIB, 19 * GIB)
    env.size_report("thread-workspace-bytes", 21 * GIB)
    env.size_report("codex-home-bytes", 3 * GIB)
    code, texts = env.run()
    joined = "\n".join(texts)
    check("root disk under 20% free alerts", "根磁碟" in joined and "19%" in joined, joined)
    check("thread workspace over 20 GB after cleanup alerts", "thread workspace" in joined, joined)
    check("CODEX_HOME over 2 GB alerts", "CODEX_HOME" in joined, joined)
    code, texts = env.run(now=NOW + HOUR)
    check("ongoing size alerts are not repeated every hour", texts == [], str(texts))
    code, texts = env.run(now=NOW + 25 * HOUR)
    check("ongoing size alerts repeat after a day", any("根磁碟" in t for t in texts), str(texts))
    reset(env)

    env.size_report("thread-workspace-bytes", GIB, at=NOW - 5 * HOUR)
    (env.state / "codex-home-bytes").unlink()
    code, texts = env.run()
    joined = "\n".join(texts)
    check("stale thread workspace report alerts (cleanup not running)", "thread workspace" in joined and "沒有回報" in joined, joined)
    check("missing CODEX_HOME report alerts", "CODEX_HOME" in joined and "沒有回報" in joined, joined)
    reset(env)


def check_log_unreadable(env: Env):
    env.write_log(steady_history(8))
    env.logs.chmod(0)
    try:
        if os.access(env.logs, os.R_OK):
            print("host-alerts.py: running as root; unreadable-log case skipped")
            return
        code, texts = env.run()
    finally:
        env.logs.chmod(0o755)
    check("unreadable Caddy log is itself an alert", any("access log" in t for t in texts), str(texts))
    reset(env)


def check_delivery(env: Env):
    env.write_log(steady_history(8))
    env.disk = Usage(100 * GIB, 90 * GIB, 10 * GIB)
    env.webhook.status = 500
    code, texts = env.run()
    env.webhook.status = 200
    check("webhook failure exits non-zero", code != 0, f"code={code}")
    code, texts = env.run(now=NOW + 60)
    check("undelivered alert is retried next run", any("根磁碟" in t for t in texts), str(texts))
    check("every message carries the [work-agent] prefix", texts and all(t.startswith("[work-agent]") for t in texts), str(texts))
    post = env.webhook.posts[-1]
    check("webhook gets JSON", post["type"] == "application/json", str(post["type"]))
    reset(env)

    env.disk = Usage(100 * GIB, 90 * GIB, 10 * GIB)
    env.hook_file.chmod(0o644)
    before = len(env.webhook.posts)
    code, _ = env.run()
    check("group/world-readable webhook file is refused", code != 0 and len(env.webhook.posts) == before)
    env.hook_file.chmod(0o600)
    env.hook_file.unlink()
    code, _ = env.run()
    check("missing webhook file fails loudly", code != 0)
    env.hook_file.write_text(env.webhook.url + "\n"); env.hook_file.chmod(0o600)
    reset(env)


def main():
    cli = load()
    tmp = Path(tempfile.mkdtemp(prefix="host-alerts-test-"))
    webhook = FakeWebhook()
    try:
        check_values_shared_with_the_clis(cli)
        env = Env(cli, tmp, webhook)
        check_quiet_run(env)
        check_responses_rate(env)
        check_image_count(env)
        check_containers(env)
        check_child_oom(env)
        check_sizes(env)
        check_log_unreadable(env)
        check_delivery(env)
    finally:
        webhook.close(); shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print("host-alerts: failed", *failures, sep="\n  - ", file=sys.stderr); return 1
    print("host alert checks passed (fixture Caddy logs, fake docker, fake webhook on 127.0.0.1)."); return 0


if __name__ == "__main__":
    sys.exit(main())
