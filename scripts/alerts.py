#!/usr/bin/env python3
"""host 每小時的告警檢查（spec #21 §8；#24 決定 4）。由 scripts/alerts.sh 從 cron 呼叫。

四類告警，有任何一條就以一則訊息 POST 到 Slack Incoming Webhook（訊息開頭 `[work-agent]`）：

1. container 被 OOMKilled、RestartCount 增加，或不在跑（`docker inspect`，不 exec 進去）。
2. 根磁碟可用 < 20%；thread workspace 清完仍 > 20 GB；CODEX_HOME > 2 GB。後兩者的大小由
   清理排程在 container 內量好寫到 state 檔（lib.sh `report_size`），這裡只讀；超過 3 小時
   沒更新本身就是告警（清理沒在跑）。
3. 上一個完整小時的 `/responses` 請求數 > 前 7 天「有請求的小時」中位數的 5 倍。log 最早
   一筆距今不滿 7 天時只記錄、不告警。
4. 當天（UTC）生圖請求 > 50。生圖請求 = User-Agent 是 `work-agent-company-image` 的
   `/responses`（agents/bin/company-image 固定送這個 header）。

③④ 讀 host Caddy 的 JSON access log（含輪替出去的 .gz）。讀不到 log 也是告警。

webhook URL 只在 host 上的檔案（預設 ~/.config/work-agent/alert-webhook，0600），不進
container、不進 repo；權限比 0600 寬就拒絕。送不出去時不更新去重狀態，下一輪會重送。

中位數只取有請求的小時：PM 只在上班時間用，168 小時裡多數是 0，照字面取中位數會是 0，
任何一個上班小時都會超過門檻。③④ 的數字是初值，上線兩週後用實際 log 調。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import stat
import statistics
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

PREFIX = "[work-agent]"
CONTAINERS = ("work-agent", "work-agent-sandbox-1", "work-agent-sandbox-2")
HOUR, DAY = 3600, 86400
GIB = 1024 ** 3
ROOT_FREE_MIN = 0.20
# 跟 agents/bin/thread-workspace 的 MAX_TOTAL_BYTES 同一個上限（tests/host-alerts.py 比對）。
THREAD_WORKSPACE_LIMIT_BYTES = 20 * GIB
CODEX_HOME_LIMIT_BYTES = 2 * GIB
REPORT_STALE_SECONDS = 3 * HOUR
RATE_FACTOR = 5
RATE_WARMUP_SECONDS = 7 * DAY
RATE_BASELINE_HOURS = 7 * 24
IMAGE_DAILY_LIMIT = 50
# 等於 agents/bin/company-image 的 USER_AGENT（tests/host-alerts.py 比對）。
IMAGE_USER_AGENT = "work-agent-company-image"
REPEAT_SECONDS = DAY
WEBHOOK_TIMEOUT_SECONDS = 15

disk_usage = shutil.disk_usage


def gib(n: float) -> str:
    return f"{n / GIB:.1f} GB"


class Checker:
    def __init__(self, state: dict, now: float):
        self.state = state
        self.now = now
        self.alerts: list[str] = []
        self.active: set[str] = set()

    def ongoing(self, key: str, message: str) -> None:
        """持續性的狀況：第一次就發，之後每 24 小時重發一次，恢復後重置。"""
        self.active.add(key)
        last = self.state.setdefault("ongoing", {}).get(key)
        if last is None or self.now - last >= REPEAT_SECONDS:
            self.state["ongoing"][key] = self.now
            self.alerts.append(message)

    def once(self, key: str, message: str) -> None:
        """一次性的事件（某次 OOM、某天超量）：同一個 key 只發一次。"""
        seen = self.state.setdefault("events", {})
        if key not in seen:
            seen[key] = self.now
            self.alerts.append(message)

    def finish(self) -> None:
        ongoing = self.state.get("ongoing", {})
        for key in list(ongoing):
            if key not in self.active:
                del ongoing[key]
        events = self.state.get("events", {})
        for key in [k for k, t in events.items() if self.now - t > 30 * DAY]:
            del events[key]


def check_containers(c: Checker) -> None:
    proc = subprocess.run(["docker", "inspect", *CONTAINERS], capture_output=True, text=True, timeout=60)
    try:
        found = {item["Name"].lstrip("/"): item for item in json.loads(proc.stdout or "[]")}
    except (ValueError, KeyError, TypeError):
        c.ongoing("docker-inspect", f"`docker inspect` 回傳看不懂的內容（exit {proc.returncode}）：{proc.stderr.strip()[:200]}")
        return
    restarts = c.state.setdefault("restart_counts", {})
    for name in CONTAINERS:
        item = found.get(name)
        if item is None:
            c.ongoing(f"missing:{name}", f"container `{name}` 不存在")
            continue
        st = item.get("State", {})
        if not st.get("Running"):
            c.ongoing(f"stopped:{name}", f"container `{name}` 沒在跑")
        if st.get("OOMKilled"):
            c.once(f"oom:{item['Id']}:{st.get('FinishedAt')}",
                   f"container `{name}` 被 OOMKilled（結束於 {st.get('FinishedAt')}）")
        count = int(item.get("RestartCount", 0))
        prev = restarts.get(name)
        if prev and prev.get("id") == item["Id"] and count > prev.get("count", 0):
            c.alerts.append(f"container `{name}` RestartCount {prev['count']} → {count}")
        restarts[name] = {"id": item["Id"], "count": count}


def check_sizes(c: Checker, state_dir: Path, disk_path: str) -> None:
    usage = disk_usage(disk_path)
    free = usage.free / usage.total if usage.total else 0
    if free < ROOT_FREE_MIN:
        c.ongoing("root-disk", f"根磁碟可用 {free:.0%}（{gib(usage.free)}／{gib(usage.total)}），低於 {ROOT_FREE_MIN:.0%}")
    for report, label, limit, what in (
        ("thread-workspace-bytes", "thread workspace", THREAD_WORKSPACE_LIMIT_BYTES, "清完仍"),
        ("codex-home-bytes", "CODEX_HOME", CODEX_HOME_LIMIT_BYTES, "目前"),
    ):
        try:
            stamp, size = (int(x) for x in (state_dir / report).read_text().split())
        except (OSError, ValueError):
            stamp, size = None, None
        if stamp is None or c.now - stamp > REPORT_STALE_SECONDS:
            c.ongoing(f"stale:{report}", f"{label} 大小超過 3 小時沒有回報（清理排程可能沒在跑，看 workspace-cleanup／artifact-cleanup log）")
        elif size > limit:
            c.ongoing(f"size:{report}", f"{label} {what} {gib(size)}，超過 {gib(limit)}")


def read_requests(log_dir: Path) -> list[tuple[float, bool]]:
    """回傳所有 POST /responses 的 (ts, 是否生圖)。其他路徑、壞掉的行略過。"""
    requests = []
    for path in sorted(log_dir.iterdir()):
        if not path.name.startswith("access") or not path.is_file():
            continue
        opener = gzip.open if path.name.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                    req = entry["request"]
                    if req.get("method") != "POST" or req.get("uri", "").split("?", 1)[0] != "/responses":
                        continue
                    agents = req.get("headers", {}).get("User-Agent") or [""]
                    requests.append((float(entry["ts"]), agents[0] == IMAGE_USER_AGENT))
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    return requests


def check_usage(c: Checker, log_dir: Path) -> None:
    try:
        requests = read_requests(log_dir)
    except OSError as exc:
        c.ongoing("proxy-log", f"讀不到 host proxy 的 access log（{log_dir}）：{exc.strerror or exc}；③④ 用量告警沒有在跑")
        return
    if requests:
        earliest = min(ts for ts, _ in requests)
        c.state["first_seen"] = min(c.state.get("first_seen", earliest), earliest)

    hour = int(c.now // HOUR * HOUR - HOUR)
    if c.state.get("rate_hour") != hour:
        c.state["rate_hour"] = hour
        buckets: dict[int, int] = {}
        for ts, _ in requests:
            buckets[int(ts // HOUR * HOUR)] = buckets.get(int(ts // HOUR * HOUR), 0) + 1
        count = buckets.get(hour, 0)
        first_seen = c.state.get("first_seen")
        if first_seen is None or hour - first_seen < RATE_WARMUP_SECONDS:
            print(f"/responses {count} requests in the last hour; warming up, not alerting")
        else:
            baseline = [n for h, n in buckets.items() if hour - RATE_BASELINE_HOURS * HOUR <= h < hour and n > 0]
            if baseline:
                median = statistics.median(baseline)
                print(f"/responses {count} requests in the last hour; median of active hours {median}")
                if count > RATE_FACTOR * median:
                    stamp = time.strftime("%Y-%m-%d %H:00 UTC", time.gmtime(hour))
                    c.alerts.append(f"/responses {stamp} 一小時 {count} 次，超過前 7 天活躍小時中位數 {median:g} 的 {RATE_FACTOR} 倍")

    day = int(c.now // DAY * DAY)
    images = sum(1 for ts, image in requests if image and day <= ts < day + DAY)
    if images > IMAGE_DAILY_LIMIT:
        c.once(f"images:{day}", f"今天（UTC {time.strftime('%Y-%m-%d', time.gmtime(day))}）生圖請求 {images} 次，超過 {IMAGE_DAILY_LIMIT}")


def read_webhook(path: Path) -> str:
    st = os.stat(path)
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise RuntimeError(f"{path} must be mode 0600 (it holds the webhook URL)")
    url = path.read_text().strip()
    parts = urlsplit(url)
    if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost"))):
        raise RuntimeError(f"{path} must hold an https:// webhook URL")
    return url


def post(url: str, alerts: list[str]) -> None:
    text = f"{PREFIX} {len(alerts)} 則告警\n" + "\n".join(f"• {a}" for a in alerts)
    request = Request(url, data=json.dumps({"text": text}).encode(), method="POST",
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"webhook returned HTTP {response.status}")


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(state, sort_keys=True))
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    parser = argparse.ArgumentParser(prog="alerts.py")
    parser.add_argument("--state-dir", type=Path, default=state_home / "work-agent")
    parser.add_argument("--log-dir", type=Path, default=Path("/var/log/work-agent-proxy"))
    parser.add_argument("--webhook-file", type=Path, default=config_home / "work-agent/alert-webhook")
    parser.add_argument("--disk-path", default="/")
    parser.add_argument("--now", type=float, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    args.state_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.state_dir / "alerts-state.json"
    previous = load_state(state_path)
    c = Checker(json.loads(json.dumps(previous)), args.now if args.now is not None else time.time())
    check_containers(c)
    check_sizes(c, args.state_dir, args.disk_path)
    check_usage(c, args.log_dir)
    c.finish()

    if not c.alerts:
        save_state(state_path, c.state)
        print("no alerts")
        return 0
    for alert in c.alerts:
        print(f"ALERT: {alert}")
    try:
        post(read_webhook(args.webhook_file), c.alerts)
    except (OSError, RuntimeError, HTTPError, URLError) as exc:
        # 不存新狀態：去重記號、RestartCount 基準都留在上一輪，下一輪會重新偵測並重送。
        print(f"FAILED: alerts not delivered: {exc}", file=sys.stderr)
        return 1
    save_state(state_path, c.state)
    print(f"delivered {len(c.alerts)} alert(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
