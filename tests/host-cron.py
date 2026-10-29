#!/usr/bin/env python3
"""host 排程（spec #21 §5、§8；#33）的離線測試。

PATH 最前面放 tests/fake-docker，跑真的 scripts/*.sh：驗每支排程 exec 進哪個 service、
跑什麼指令、失敗時 log 寫 FAILED 並回非 0、量到的大小寫到哪個 state 檔給告警讀。
CODEX_HOME sessions 清理由假 docker 把 container 路徑換成 tmp 目錄、在本機真的跑
`find`，所以「`log/` 與 sqlite 不動」是實際刪除結果，不是比對指令字串。

證明不了：crontab 真的觸發、真的 container 裡有這些指令與權限。那些在 #35 人工驗。
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE_DOCKER = ROOT / "tests/fake-docker"
DAY = 86400
# 換格只看「exec 進不進得去」：先在那格跑 `true`，進得去才在那格跑真正的指令。
PROBE1 = "prototype-sandbox-1 true"
PROBE2 = "prototype-sandbox-2 true"
failures: list[str] = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}" if detail else name)


class Host:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "bin"; self.bin.mkdir()
        os.symlink(FAKE_DOCKER, self.bin / "docker")
        self.state = tmp / "state"
        self.docker_log = tmp / "docker.log"

    def run(self, script: str, canned=None, pathmap=None, extra_env=None):
        self.docker_log.write_text("")
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.tmp / "home"),
            "XDG_STATE_HOME": str(self.state),
            "FAKE_DOCKER_LOG": str(self.docker_log),
            "FAKE_DOCKER_CANNED": json.dumps(canned or {}),
            "FAKE_DOCKER_PATHMAP": json.dumps(pathmap or {}),
            **(extra_env or {}),
        }
        proc = subprocess.run([str(ROOT / "scripts" / script)], env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
        calls = [json.loads(line) for line in self.docker_log.read_text().splitlines()]
        return proc.returncode, proc.stdout, calls

    def log(self, name: str) -> str:
        path = self.state / "work-agent" / name
        return path.read_text() if path.exists() else ""

    def report(self, name: str) -> list[str]:
        path = self.state / "work-agent" / name
        return path.read_text().split() if path.exists() else []


def exec_target(call):
    """回傳 (service, cmd)；不是 compose exec 就回 None。"""
    if call[:1] != ["compose"] or "exec" not in call:
        return None
    rest = call[call.index("exec") + 1:]
    while rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in ("-w", "--workdir"):
            rest.pop(0)
    return rest[0], rest[1:]


def age(path: Path, days: float):
    when = time.time() - days * DAY
    os.utime(path, (when, when), follow_symlinks=False)


def check_artifact_cleanup(host: Host):
    codex = host.tmp / "codex"
    day_old = codex / "sessions/2026/10/01"; day_old.mkdir(parents=True)
    day_new = codex / "sessions/2026/10/28"; day_new.mkdir(parents=True)
    (day_old / "rollout-2026-10-01T00-00-00-a.jsonl").write_text("{}\n"); age(day_old / "rollout-2026-10-01T00-00-00-a.jsonl", 8)
    (day_new / "rollout-2026-10-28T00-00-00-b.jsonl").write_text("{}\n"); age(day_new / "rollout-2026-10-28T00-00-00-b.jsonl", 6)
    (codex / "log").mkdir(); (codex / "log/codex-tui.log").write_text("x\n"); age(codex / "log/codex-tui.log", 30)
    (codex / "state_5.sqlite").write_text("db"); age(codex / "state_5.sqlite", 30)
    (codex / "state_5.sqlite-wal").write_text("wal"); age(codex / "state_5.sqlite-wal", 30)
    (codex / "sessions/notes.txt").write_text("not a rollout"); age(codex / "sessions/notes.txt", 30)
    slack_list = "backlog-agent /home/node/code/agent-config/skills/slack-list/scripts/slack-list cleanup"
    code, out, calls = host.run("cleanup-artifacts.sh", canned={slack_list: {"stdout": "cleaned\n"}},
                                pathmap={"/home/node/.codex": str(codex)})
    log = host.log("artifact-cleanup.log")
    check("artifact cleanup exits 0", code == 0, log + out)
    targets = [exec_target(c) for c in calls]
    check("drafts still cleaned by agent-config slack-list cleanup",
          ("backlog-agent", ["/home/node/code/agent-config/skills/slack-list/scripts/slack-list", "cleanup"]) in targets, str(targets))
    check("all execs go into the bot service", targets and all(t and t[0] == "backlog-agent" for t in targets), str(targets))
    check("rollout older than 7 days removed", not (day_old / "rollout-2026-10-01T00-00-00-a.jsonl").exists())
    check("6-day-old rollout kept", (day_new / "rollout-2026-10-28T00-00-00-b.jsonl").exists())
    check("log/ untouched", (codex / "log/codex-tui.log").exists())
    check("state sqlite untouched", (codex / "state_5.sqlite").exists() and (codex / "state_5.sqlite-wal").exists())
    check("non-rollout file under sessions untouched", (codex / "sessions/notes.txt").exists())
    report = host.report("codex-home-bytes")
    check("CODEX_HOME size reported for the alert", len(report) == 2 and int(report[1]) > 0, str(report))

    # slack-list 失敗：記 FAILED、回非 0，但 sessions 清理照跑。
    (day_new / "rollout-old-again.jsonl").write_text("{}\n")
    (day_new / "rollout-2026-10-20T00-00-00-c.jsonl").write_text("{}\n"); age(day_new / "rollout-2026-10-20T00-00-00-c.jsonl", 9)
    code, out, calls = host.run("cleanup-artifacts.sh", canned={slack_list: {"exit": 4}},
                                pathmap={"/home/node/.codex": str(codex)})
    log = host.log("artifact-cleanup.log")
    check("slack-list failure exits non-zero", code != 0)
    check("slack-list failure logged as FAILED", "FAILED" in log, log)
    check("sessions cleanup still runs after a drafts failure", not (day_new / "rollout-2026-10-20T00-00-00-c.jsonl").exists())


def check_workspace_cleanup(host: Host):
    tw = "backlog-agent thread-workspace cleanup"
    out1 = "prototype-sandbox-1 sandbox-runner cleanup-out"
    out2 = "prototype-sandbox-2 sandbox-runner cleanup-out"
    code, out, calls = host.run("cleanup-workspaces.sh", canned={
        tw: {"stdout": "removed C0123456789-1710000000.000001 reason=age bytes=10\nremaining_bytes=12345\n"},
        PROBE1: {},
        out1: {"stdout": "cleanup-out: removed 2 entries\n"},
    })
    log = host.log("workspace-cleanup.log")
    targets = [exec_target(c) for c in calls]
    check("workspace cleanup exits 0", code == 0, log)
    check("thread workspace cleaned inside the bot", ("backlog-agent", ["thread-workspace", "cleanup"]) in targets, str(targets))
    check("out cleaned inside sandbox 1", ("prototype-sandbox-1", ["sandbox-runner", "cleanup-out"]) in targets, str(targets))
    check("sandbox 2 not needed when sandbox 1 works", not any(t and t[0] == "prototype-sandbox-2" for t in targets))
    report = host.report("thread-workspace-bytes")
    check("remaining thread workspace bytes reported", len(report) == 2 and report[1] == "12345", str(report))
    check("cleanup output kept in the log", "reason=age" in log, log)

    code, out, calls = host.run("cleanup-workspaces.sh", canned={
        tw: {"stdout": "removed x\nremaining_bytes=999\n", "stderr": "FAILED to notify x\n", "exit": 3},
        PROBE1: {"exit": 1, "stderr": "service \"prototype-sandbox-1\" is not running\n"},
        PROBE2: {},
        out2: {"stdout": "cleanup-out: removed 0 entries\n"},
    })
    log = host.log("workspace-cleanup.log")
    targets = [exec_target(c) for c in calls]
    check("thread-workspace failure exits non-zero", code != 0)
    check("thread-workspace failure logged as FAILED", "FAILED: thread-workspace cleanup" in log, log)
    check("remaining bytes still reported after a notify failure", host.report("thread-workspace-bytes")[1:] == ["999"])
    check("out cleanup falls back to sandbox 2 when sandbox 1 cannot be exec'd",
          ("prototype-sandbox-2", ["sandbox-runner", "cleanup-out"]) in targets, str(targets))
    check("sandbox 1 command not run when exec into it fails",
          ("prototype-sandbox-1", ["sandbox-runner", "cleanup-out"]) not in targets, str(targets))

    # 進得去第 1 格、但 cleanup-out 自己失敗：那是指令的錯，不換第 2 格重跑。
    code, out, calls = host.run("cleanup-workspaces.sh", canned={
        tw: {"stdout": "remaining_bytes=1\n"},
        PROBE1: {},
        out1: {"exit": 1, "stderr": "cleanup-out: permission denied\n"},
        out2: {"stdout": "should not run\n"},
    })
    log = host.log("workspace-cleanup.log")
    targets = [exec_target(c) for c in calls]
    check("cleanup-out failure inside sandbox 1 exits non-zero", code != 0, log)
    check("cleanup-out failure inside sandbox 1 logged as FAILED", "FAILED" in log and "cleanup-out" in log, log)
    check("cleanup-out failure inside sandbox 1 is not retried in sandbox 2",
          not any(t and t[0] == "prototype-sandbox-2" for t in targets), str(targets))


def check_pnpm_prune(host: Host):
    snapshots = host.tmp / "snapshots"
    for repo, pm in (("frontend", "pnpm@10.12.1+sha512.abc"), ("backend", "pnpm@10.12.1"), ("docs", "pnpm@9.15.0"), ("tool", "npm@10.0.0")):
        (snapshots / repo).mkdir(parents=True)
        (snapshots / repo / "package.json").write_text(json.dumps({"name": repo, "packageManager": pm}))
    (snapshots / "agent-config").mkdir()
    code, out, calls = host.run("prune-pnpm-store.sh", canned={PROBE1: {}, "prototype-sandbox-1 corepack": {"stdout": "pruned\n"}},
                                extra_env={"SNAPSHOT_ROOT": str(snapshots)})
    log = host.log("pnpm-prune.log")
    targets = [exec_target(c) for c in calls]
    prunes = [t for t in targets if t and t[1] != ["true"]]
    check("pnpm prune exits 0", code == 0, log)
    check("prunes with every pnpm version the snapshots pin, once each",
          sorted(t[1] for t in prunes) == [["corepack", "pnpm@10.12.1", "store", "prune"], ["corepack", "pnpm@9.15.0", "store", "prune"]],
          str(targets))
    check("prune runs in a sandbox, never in the bot", all(t and t[0].startswith("prototype-sandbox-") for t in targets), str(targets))

    # prune 自己失敗：記 FAILED，不換第 2 格重跑。
    code, out, calls = host.run("prune-pnpm-store.sh", canned={
        PROBE1: {}, "prototype-sandbox-1 corepack": {"exit": 1}, "prototype-sandbox-2 corepack": {"stdout": "pruned\n"},
    }, extra_env={"SNAPSHOT_ROOT": str(snapshots)})
    log = host.log("pnpm-prune.log")
    targets = [exec_target(c) for c in calls]
    check("prune failure exits non-zero", code != 0, log)
    check("prune failure logged as FAILED", "FAILED: pnpm@" in log, log)
    check("prune failure inside sandbox 1 is not retried in sandbox 2",
          not any(t and t[0] == "prototype-sandbox-2" for t in targets), str(targets))

    # 第 1 格 exec 不進去：整輪改在第 2 格跑。
    code, out, calls = host.run("prune-pnpm-store.sh", canned={
        PROBE1: {"exit": 1}, PROBE2: {}, "prototype-sandbox-2 corepack": {"stdout": "pruned\n"},
    }, extra_env={"SNAPSHOT_ROOT": str(snapshots)})
    log = host.log("pnpm-prune.log")
    targets = [exec_target(c) for c in calls]
    check("prune falls back to sandbox 2 when sandbox 1 cannot be exec'd", code == 0, log)
    check("prune runs only in sandbox 2 after the fallback",
          sorted(t[1] for t in targets if t and t[0] == "prototype-sandbox-2" and t[1] != ["true"]) ==
          [["corepack", "pnpm@10.12.1", "store", "prune"], ["corepack", "pnpm@9.15.0", "store", "prune"]], str(targets))

    # 兩格都進不去：FAILED。
    code, out, calls = host.run("prune-pnpm-store.sh", canned={PROBE1: {"exit": 1}, PROBE2: {"exit": 1}},
                                extra_env={"SNAPSHOT_ROOT": str(snapshots)})
    check("no reachable sandbox fails the prune", code != 0 and "FAILED" in host.log("pnpm-prune.log"), host.log("pnpm-prune.log"))


def check_alerts_wrapper(host: Host):
    """alerts.sh 把 state dir、webhook 檔與 log dir 交給 alerts.py；送不出去記 FAILED。

    這裡沒有 webhook：state 檔不存在 → 「沒有回報」告警 → 送不出去 → exit 非 0。
    告警判斷本身在 tests/host-alerts.py。
    """
    inspect = host.tmp / "inspect.json"
    inspect.write_text(json.dumps({n: {"Id": n, "Name": "/" + n, "RestartCount": 0, "State": {"Running": True}}
                                   for n in ("work-agent", "work-agent-sandbox-1", "work-agent-sandbox-2")}))
    logs = host.tmp / "proxy-logs"; logs.mkdir()
    for report in ("thread-workspace-bytes", "codex-home-bytes"):
        (host.state / "work-agent" / report).unlink(missing_ok=True)
    code, out, calls = host.run("alerts.sh", extra_env={
        "FAKE_DOCKER_INSPECT": str(inspect),
        "WORK_AGENT_ALERT_WEBHOOK_FILE": str(host.tmp / "no-such-webhook"),
        "WORK_AGENT_PROXY_LOG_DIR": str(logs),
    })
    log = host.log("alerts.log")
    check("undeliverable alerts exit non-zero", code != 0, log)
    check("alerts.sh logs FAILED with the webhook file it tried", "FAILED: alerts not delivered" in log and "no-such-webhook" in log, log)
    check("alerts only inspect containers, never exec into them", calls and all(c[0] == "inspect" for c in calls), str(calls))
    check("alert state lives in the cron state dir", not (host.state / "work-agent/alerts-state.json").exists(),
          "state must not be saved when delivery failed")


def check_crontab():
    proc = subprocess.run(["bash", "-c", 'source "$1/scripts/lib.sh"; render_crontab "$1" /snap', "_", str(ROOT)],
                          capture_output=True, text=True, check=True)
    lines = proc.stdout.splitlines()
    def entry(marker):
        return [l for l in lines if l.endswith(marker)]
    for marker, script in (("# work-agent-snapshots", "update-snapshots.sh"),
                           ("# work-agent-artifact-cleanup", "cleanup-artifacts.sh"),
                           ("# work-agent-workspace-cleanup", "cleanup-workspaces.sh"),
                           ("# work-agent-pnpm-prune", "prune-pnpm-store.sh"),
                           ("# work-agent-alerts", "alerts.sh")):
        found = entry(marker)
        check(f"crontab has one {marker} entry", len(found) == 1, proc.stdout)
        if found:
            check(f"{marker} runs scripts/{script}", f"{ROOT}/scripts/{script}" in found[0], found[0])
            check(f"scripts/{script} is executable", os.access(ROOT / "scripts" / script, os.X_OK))
    hourly = {m: entry(m)[0].split()[:5] for m in ("# work-agent-workspace-cleanup", "# work-agent-alerts") if entry(m)}
    for marker, fields in hourly.items():
        check(f"{marker} runs every hour", fields[1:] == ["*", "*", "*", "*"], str(fields))
    if len(hourly) == 2:
        check("alerts run after the workspace cleanup in the same hour",
              int(hourly["# work-agent-alerts"][0]) > int(hourly["# work-agent-workspace-cleanup"][0]), str(hourly))
    weekly = entry("# work-agent-pnpm-prune")
    check("pnpm prune is weekly", bool(weekly) and weekly[0].split()[2:5] == ["*", "*", "0"], str(weekly))
    installer = (ROOT / "scripts/install-sync-cron.sh").read_text()
    check("installer rewrites every marker it renders", "${CRON_MARKERS[@]}" in installer or "CRON_MARKERS" in installer)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="host-cron-test-"))
    try:
        host = Host(tmp)
        check_artifact_cleanup(host)
        check_workspace_cleanup(host)
        check_pnpm_prune(host)
        check_alerts_wrapper(host)
        check_crontab()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print("host-cron: failed", *failures, sep="\n  - ", file=sys.stderr); return 1
    print("host cron checks passed (fake docker on PATH; no container, no crontab touched)."); return 0


if __name__ == "__main__":
    sys.exit(main())
