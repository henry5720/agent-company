#!/usr/bin/env python3
"""host proxy 與防火牆的離線測試（spec #21 §2、§4；#25）。

不碰真的 iptables：PATH 前面放一支假的 `iptables`，狀態存在 temp JSON。測試從
`scripts/host-firewall.sh` 的公開介面（apply／check／remove）進去，再用一個小型
封包評估器回答「這個封包會被放行還是丟掉」，所以斷言的是行為，不是規則字串。

證明不了的：真的 kernel 會不會照這樣比對、Docker 自己的鏈長什麼樣、Caddy 真的
轉得到 gateway。那些是 docs/runbook.md「host proxy 與防火牆」的人工驗證。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIREWALL = ROOT / "scripts/host-firewall.sh"
NETWORK_ENV = ROOT / "config/host-network.env"
CADDYFILE = ROOT / "config/caddy/Caddyfile"
failures: list[str] = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}" if detail else name)


def load_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if re.match(r"^[A-Z_][A-Z0-9_]*=", line):
            key, value = line.split("=", 1)
            values[key] = value
    return values


NET = load_env(NETWORK_ENV)

# 假的 iptables：只實作 host-firewall.sh 會用到的子集。規則以「參數字串」存，
# -C／-D 用完全相同的參數比對（真的 iptables 比的是 kernel 內的規則，結果一樣）。
FAKE_IPTABLES = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_IPTABLES_STATE"]
with open(state_path) as f:
    state = json.load(f)
args = [a for a in sys.argv[1:] if a not in ("-w",)]
with open(state_path + ".log", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
if args[:2] == ["-t", "filter"]:
    args = args[2:]
chains = state["chains"]
builtin = ("INPUT", "FORWARD", "OUTPUT")
def save():
    with open(state_path, "w") as f:
        json.dump(state, f)
def need(chain):
    if chain not in chains:
        sys.stderr.write(f"iptables: No chain/target/match by that name: {chain}\n")
        sys.exit(1)
op, rest = args[0], args[1:]
if op == "-S":
    names = rest[:1] or list(chains)
    for name in names:
        need(name)
        print(f"-P {name} ACCEPT" if name in builtin else f"-N {name}")
        for rule in chains[name]:
            print(f"-A {name} {rule}")
elif op == "-N":
    if rest[0] in chains:
        sys.stderr.write("iptables: Chain already exists.\n"); sys.exit(1)
    chains[rest[0]] = []; save()
elif op == "-L":
    need(rest[0])
elif op == "-F":
    need(rest[0]); chains[rest[0]] = []; save()
elif op == "-X":
    need(rest[0])
    if chains[rest[0]]:
        sys.stderr.write("iptables: Directory not empty.\n"); sys.exit(1)
    for name, rules in chains.items():
        if any(r.endswith("-j " + rest[0]) for r in rules):
            sys.stderr.write("iptables: Too many links.\n"); sys.exit(1)
    del chains[rest[0]]; save()
elif op == "-A":
    need(rest[0]); chains[rest[0]].append(" ".join(rest[1:])); save()
elif op == "-I":
    need(rest[0])
    pos, spec = (int(rest[1]), rest[2:]) if len(rest) > 1 and rest[1].isdigit() else (1, rest[1:])
    chains[rest[0]].insert(pos - 1, " ".join(spec)); save()
elif op in ("-C", "-D"):
    need(rest[0])
    rules = chains[rest[0]]
    if op == "-D" and len(rest) == 2 and rest[1].isdigit():
        index = int(rest[1]) - 1
        if index >= len(rules):
            sys.exit(1)
        del rules[index]; save(); sys.exit(0)
    spec = " ".join(rest[1:])
    if spec not in rules:
        sys.stderr.write("iptables: Bad rule (does a matching rule exist in that chain?).\n")
        sys.exit(1)
    if op == "-D":
        rules.remove(spec); save()
else:
    sys.stderr.write(f"fake iptables: unsupported {args}\n"); sys.exit(2)
'''


class Host:
    """一台假 host：Docker 已經建好 DOCKER-USER，ufw 已經在 INPUT 掛了自己的鏈。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        bindir = tmp / "bin"
        bindir.mkdir()
        shim = bindir / "iptables"
        shim.write_text(FAKE_IPTABLES)
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        self.state_path = tmp / "iptables.json"
        self.state_path.write_text(json.dumps({"chains": {
            "INPUT": ["-j ufw-before-input"],
            "FORWARD": ["-j DOCKER-USER", "-j DOCKER-FORWARD"],
            "OUTPUT": [],
            "DOCKER-USER": [],
            "DOCKER-FORWARD": [],
            "ufw-before-input": [],
        }}))
        self.env = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "FAKE_IPTABLES_STATE": str(self.state_path),
            "WORK_AGENT_NETWORK_ENV": str(NETWORK_ENV),
            # 讓 br_netfilter 檢查讀假檔，不碰 /proc。
            "WORK_AGENT_BRIDGE_NF_PATH": str(tmp / "bridge-nf-call-iptables"),
        }
        (tmp / "bridge-nf-call-iptables").write_text("1\n")

    def run(self, *args):
        return subprocess.run(["bash", str(FIREWALL), *args], env=self.env,
                              capture_output=True, text=True)

    @property
    def chains(self) -> dict[str, list[str]]:
        return json.loads(self.state_path.read_text())["chains"]

    def log(self) -> list[str]:
        path = Path(str(self.state_path) + ".log")
        return path.read_text().splitlines() if path.exists() else []


# --- 封包評估器 -------------------------------------------------------------

def tokens(rule: str) -> list[str]:
    return rule.split()


def matches(rule: str, pkt: dict) -> bool:
    t = tokens(rule)
    i = 0
    while i < len(t):
        neg = t[i] == "!"
        if neg:
            i += 1
        opt = t[i]
        if opt == "-j":
            return True
        if opt == "-m":
            i += 2
            continue
        val = t[i + 1]
        if opt in ("-s", "-d"):
            addr = pkt["src"] if opt == "-s" else pkt["dst"]
            hit = ipaddress.ip_address(addr) in ipaddress.ip_network(val, strict=False)
        elif opt == "-i":
            hit = pkt.get("in") == val
        elif opt == "-o":
            hit = pkt.get("out") == val
        elif opt == "-p":
            hit = pkt["proto"] == val
        elif opt == "--dport":
            hit = str(pkt.get("dport")) == val
        elif opt == "--ctstate":
            hit = pkt.get("state", "NEW") in val.split(",")
        else:
            raise AssertionError(f"evaluator does not know {opt} in {rule!r}")
        if hit == neg:
            return False
        i += 2
    return True


def walk(chains, chain, pkt) -> str | None:
    """回傳 ACCEPT／DROP；走完沒有定論回傳 None（交給下一層）。"""
    for rule in chains[chain]:
        if not matches(rule, pkt):
            continue
        target = tokens(rule)[tokens(rule).index("-j") + 1]
        if target in ("ACCEPT", "DROP"):
            return target
        if target == "RETURN":
            return None
        if target in chains:
            verdict = walk(chains, target, pkt)
            if verdict:
                return verdict
    return None


def forward(chains, pkt) -> str:
    """FORWARD：先過 DOCKER-USER，再模擬 Docker 自己的鏈（enable_icc=false）。"""
    verdict = walk(chains, "DOCKER-USER", pkt)
    if verdict:
        return verdict
    if pkt.get("state", "NEW") != "NEW":
        return "ACCEPT"  # DOCKER-CT
    bridge = NET["WORK_AGENT_BRIDGE"]
    if pkt.get("in") == bridge and pkt.get("out") == bridge:
        return "DROP"  # enable_icc=false
    return "ACCEPT"  # -i br ! -o br -j ACCEPT（往外網）


def host_input(chains, pkt) -> str:
    verdict = walk(chains, "INPUT", pkt)
    return verdict or "HOST-DEFAULT"


BR = NET["WORK_AGENT_BRIDGE"]
BOT = NET["WORK_AGENT_BOT_IP"]
GW = NET["WORK_AGENT_GATEWAY_IP"]
SANDBOX = str(ipaddress.ip_network(NET["WORK_AGENT_IP_RANGE"])[5])
OTHER_SANDBOX = str(ipaddress.ip_network(NET["WORK_AGENT_IP_RANGE"])[6])
PROXY_PORT = int(NET["WORK_AGENT_PROXY_PORT"])
RUNNER_PORT = int(NET["WORK_AGENT_RUNNER_PORT"])


def fwd(src, dst, out="ens5", dport=443, state="NEW"):
    return {"src": src, "dst": dst, "in": BR, "out": out, "proto": "tcp", "dport": dport, "state": state}


def inp(src, dport, dst=GW, state="NEW"):
    return {"src": src, "dst": dst, "in": BR, "proto": "tcp", "dport": dport, "state": state}


def test_policy_after_apply():
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        result = host.run("apply")
        check("apply exits 0", result.returncode == 0, result.stderr)
        c = host.chains
        cases = [
            ("sandbox -> internet", forward(c, fwd(SANDBOX, "1.1.1.1")), "ACCEPT"),
            ("sandbox -> public DNS udp", forward(c, {**fwd(SANDBOX, "8.8.8.8", dport=53), "proto": "udp"}), "ACCEPT"),
            ("sandbox -> IMDS", forward(c, fwd(SANDBOX, "169.254.169.254", dport=80)), "DROP"),
            ("sandbox -> VPC", forward(c, fwd(SANDBOX, "172.31.0.2", dport=53)), "DROP"),
            ("sandbox -> 10/8", forward(c, fwd(SANDBOX, "10.0.0.5")), "DROP"),
            ("sandbox -> 192.168/16", forward(c, fwd(SANDBOX, "192.168.1.1")), "DROP"),
            ("sandbox -> tailnet", forward(c, fwd(SANDBOX, "100.98.146.109", out="tailscale0")), "DROP"),
            ("sandbox -> other docker net", forward(c, fwd(SANDBOX, "172.18.0.2", out="br-other")), "DROP"),
            ("sandbox -> bot", forward(c, fwd(SANDBOX, BOT, out=BR, dport=22)), "DROP"),
            ("sandbox -> other sandbox runner", forward(c, fwd(SANDBOX, OTHER_SANDBOX, out=BR, dport=RUNNER_PORT)), "DROP"),
            ("sandbox reply to bot", forward(c, fwd(SANDBOX, BOT, out=BR, dport=40000, state="ESTABLISHED")), "ACCEPT"),
            ("bot -> sandbox runner", forward(c, fwd(BOT, SANDBOX, out=BR, dport=RUNNER_PORT)), "ACCEPT"),
            ("bot -> sandbox other port", forward(c, fwd(BOT, SANDBOX, out=BR, dport=5173)), "DROP"),
            ("bot -> internet (Slack)", forward(c, fwd(BOT, "1.1.1.1")), "ACCEPT"),
            ("bot -> proxy", host_input(c, inp(BOT, PROXY_PORT)), "ACCEPT"),
            ("bot -> host ssh", host_input(c, inp(BOT, 22)), "DROP"),
            ("bot -> proxy port on host's VPC address", host_input(c, inp(BOT, PROXY_PORT, dst="172.31.10.168")), "DROP"),
            ("sandbox -> proxy", host_input(c, inp(SANDBOX, PROXY_PORT)), "DROP"),
            ("sandbox -> host VPC address", host_input(c, inp(SANDBOX, 22, dst="172.31.10.168")), "DROP"),
            ("host-initiated reply from sandbox", host_input(c, inp(SANDBOX, 45000, state="ESTABLISHED")), "ACCEPT"),
            ("other bridge untouched", host_input(c, {**inp(SANDBOX, PROXY_PORT), "in": "docker0"}), "HOST-DEFAULT"),
        ]
        for name, got, want in cases:
            check(name, got == want, f"got {got}, want {want}")


def test_apply_is_idempotent_and_first():
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        host.run("apply")
        first = host.chains
        again = host.run("apply")
        check("second apply exits 0", again.returncode == 0, again.stderr)
        check("second apply changes nothing", host.chains == first)
        check("check passes after apply", host.run("check").returncode == 0)
        jumps_in = [r for r in first["INPUT"] if "WORK-AGENT" in r]
        jumps_fwd = [r for r in first["DOCKER-USER"] if "WORK-AGENT" in r]
        check("exactly one INPUT jump", len(jumps_in) == 1, str(first["INPUT"]))
        check("exactly one DOCKER-USER jump", len(jumps_fwd) == 1, str(first["DOCKER-USER"]))
        check("INPUT jump comes before ufw", first["INPUT"][0] == jumps_in[0], str(first["INPUT"]))

        # 有人（例如 ufw reload 或手動）把一條 ACCEPT 插到我們前面：重下要搶回第一位。
        state = json.loads(host.state_path.read_text())
        state["chains"]["INPUT"].insert(0, "-j ACCEPT")
        state["chains"]["DOCKER-USER"].insert(0, "-j ACCEPT")
        host.state_path.write_text(json.dumps(state))
        check("check fails when jump is shadowed", host.run("check").returncode != 0)
        host.run("apply")
        after = host.chains
        check("INPUT jump back to first", "WORK-AGENT" in after["INPUT"][0], str(after["INPUT"]))
        check("DOCKER-USER jump back to first", "WORK-AGENT" in after["DOCKER-USER"][0], str(after["DOCKER-USER"]))
        check("no duplicate jumps", sum("WORK-AGENT" in r for r in after["INPUT"]) == 1, str(after["INPUT"]))

        # 鏈內容被改過：check 抓得到，apply 修得回來。
        state = json.loads(host.state_path.read_text())
        drifted = next(n for n in state["chains"] if n.startswith("WORK-AGENT-FORWARD"))
        state["chains"][drifted] = state["chains"][drifted][:1]
        host.state_path.write_text(json.dumps(state))
        check("check detects drift", host.run("check").returncode != 0)
        host.run("apply")
        check("apply repairs drift", host.run("check").returncode == 0)


def test_check_and_remove():
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        check("check fails before apply", host.run("check").returncode != 0)
        host.run("apply")
        removed = host.run("remove")
        check("remove exits 0", removed.returncode == 0, removed.stderr)
        c = host.chains
        check("remove deletes our chains", not any(n.startswith("WORK-AGENT") for n in c), str(list(c)))
        check("remove leaves ufw jump", c["INPUT"] == ["-j ufw-before-input"], str(c["INPUT"]))
        check("remove twice is fine", host.run("remove").returncode == 0)


def test_applies_before_docker_starts():
    """開機時 unit 排在 docker.service 之前：DOCKER-USER 還不存在也要先把規則下好，
    container 一被 Docker 拉起來就受管。"""
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        state = json.loads(host.state_path.read_text())
        del state["chains"]["DOCKER-USER"]
        state["chains"]["FORWARD"] = []
        host.state_path.write_text(json.dumps(state))
        result = host.run("apply")
        check("apply before docker exits 0", result.returncode == 0, result.stderr)
        check("check fails while FORWARD has no DOCKER-USER jump", host.run("check").returncode != 0)
        # Docker 起來後自己把 FORWARD → DOCKER-USER 接上，不清 DOCKER-USER 內容。
        state = json.loads(host.state_path.read_text())
        state["chains"]["FORWARD"] = ["-j DOCKER-USER", "-j DOCKER-FORWARD"]
        host.state_path.write_text(json.dumps(state))
        check("check passes once docker hooks DOCKER-USER", host.run("check").returncode == 0)
        c = host.chains
        check("policy holds after docker start", forward(c, fwd(SANDBOX, "169.254.169.254", dport=80)) == "DROP")


def test_warns_without_br_netfilter():
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        (Path(raw) / "bridge-nf-call-iptables").unlink()
        result = host.run("apply")
        check("apply still succeeds without br_netfilter", result.returncode == 0, result.stderr)
        check("warns about br_netfilter", "br_netfilter" in result.stderr, result.stderr)


def test_never_unloads_br_netfilter():
    text = FIREWALL.read_text()
    # 指令位置的 modprobe（行首、`;`／`&&`／`|` 之後、`$(` 裡）；警告訊息裡的字樣不算。
    command = re.compile(r"(^|[;&|]|\$\()\s*(sudo\s+)?modprobe\b", re.M)
    check("firewall never runs modprobe", not command.search(text))


def test_caddyfile_contract():
    text = CADDYFILE.read_text() if CADDYFILE.exists() else ""
    check("Caddyfile exists", bool(text))
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    check("admin API off", re.search(r"^\s*admin off\s*$", code, re.M))
    check("binds only the gateway IP", re.search(r"^\s*bind \{\$WORK_AGENT_GATEWAY_IP\}\s*$", code, re.M))
    check("port from host-network.env", re.search(r"^:\{\$WORK_AGENT_PROXY_PORT\} \{", code, re.M))
    check("JSON access log", re.search(r"^\s*format json\s*$", code, re.M))
    check("log kept 14 days", re.search(r"^\s*roll_keep_for 336h\s*$", code, re.M))
    check("log rolls daily so old lines can expire", re.search(r"^\s*roll_interval 24h\s*$", code, re.M))
    check("only POST /responses is routed", re.findall(r"^\s*path (.+)$", code, re.M) == ["/responses"]
          and re.findall(r"^\s*method (.+)$", code, re.M) == ["POST"])
    check("key only from env placeholder", "{env.COMPANY_GATEWAY_API_KEY}" in code
          and not re.search(r"Bearer (?!\{env\.COMPANY_GATEWAY_API_KEY\})", code))
    # 生圖走同一個 endpoint：company-image 打的就是 base + /responses。
    image_cli = (ROOT / "agents/bin/company-image").read_text()
    check("company-image only calls /responses",
          'return base.rstrip("/") + "/responses"' in image_cli
          and len(re.findall(r'"/[a-z_/]+"', image_cli.split("def gateway_endpoint", 1)[1].split("\ndef ", 1)[0])) == 1)


def test_network_env_consistent():
    subnet = ipaddress.ip_network(NET["WORK_AGENT_SUBNET"])
    pool = ipaddress.ip_network(NET["WORK_AGENT_IP_RANGE"])
    gw = ipaddress.ip_address(NET["WORK_AGENT_GATEWAY_IP"])
    bot = ipaddress.ip_address(NET["WORK_AGENT_BOT_IP"])
    check("ip range inside subnet", pool.subnet_of(subnet))
    check("gateway and bot inside subnet", gw in subnet and bot in subnet)
    check("fixed IPs outside dynamic range", gw not in pool and bot not in pool)
    check("bridge name fits IFNAMSIZ", len(NET["WORK_AGENT_BRIDGE"]) <= 15)
    check("subnet is private so sandboxes cannot reach it", subnet.is_private)
    runner = (ROOT / "agents/bin/sandbox-run").read_text()
    urls = re.search(r'DEFAULT_RUNNER_URLS = "([^"]+)"', runner).group(1).split()
    check("sandbox-run uses the runner port",
          all(u.rsplit(":", 1)[1] == NET["WORK_AGENT_RUNNER_PORT"] for u in urls), str(urls))


def test_caddy_routes_live():
    """有 caddy binary（PATH 或 CADDY_BIN）時真的起一個 proxy 打假 upstream。"""
    import http.server
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request

    caddy = os.environ.get("CADDY_BIN") or shutil.which("caddy") or shutil.which("work-agent-caddy")
    if not caddy:
        print("host-network.py: caddy not found; live route test skipped")
        return
    seen: list[dict] = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                         "host": self.headers.get("Host"), "body": self.rfile.read(length)})
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_GET = do_POST

        def log_message(self, *_):
            pass

    def free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    fmt = subprocess.run([caddy, "fmt", "--diff", str(CADDYFILE)], capture_output=True, text=True)
    check("Caddyfile is caddy-fmt clean", fmt.returncode == 0, fmt.stdout[-500:])
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    port = free_port()
    with tempfile.TemporaryDirectory() as raw:
        env = {**os.environ, "WORK_AGENT_GATEWAY_IP": "127.0.0.1", "WORK_AGENT_PROXY_PORT": str(port),
               "LOGS_DIRECTORY": raw, "XDG_DATA_HOME": raw, "XDG_CONFIG_HOME": raw, "HOME": raw,
               "COMPANY_GATEWAY_ORIGIN": f"http://127.0.0.1:{upstream.server_port}",
               "COMPANY_GATEWAY_PATH_PREFIX": "/gw", "COMPANY_GATEWAY_API_KEY": "sk-test-secret"}
        proc = subprocess.Popen([caddy, "run", "--config", str(CADDYFILE), "--adapter", "caddyfile"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            base = f"http://127.0.0.1:{port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                    break
                except OSError:
                    time.sleep(0.05)

            def call(method, path, auth=None):
                req = urllib.request.Request(base + path, method=method,
                                             data=b'{"x":1}' if method == "POST" else None)
                if auth:
                    req.add_header("Authorization", auth)
                try:
                    with opener.open(req, timeout=5) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    return e.code
                except urllib.error.URLError as e:
                    return f"unreachable: {e.reason}"

            check("POST /responses proxied", call("POST", "/responses", auth="Bearer client-junk") == 200)
            check("upstream got prefixed path", seen and seen[-1]["path"] == "/gw/responses", str(seen[-1:]))
            check("key injected, client auth replaced", seen and seen[-1]["auth"] == "Bearer sk-test-secret")
            check("Host rewritten to upstream", seen and seen[-1]["host"] == f"127.0.0.1:{upstream.server_port}")
            hits = len(seen)
            for method, path in (("GET", "/models"), ("GET", "/responses"), ("POST", "/v1/responses"),
                                 ("POST", "/responses/x"), ("POST", "/images/generations"),
                                 ("POST", "/chat/completions"), ("POST", "/%2e%2e/models")):
                check(f"{method} {path} is 403", call(method, path) == 403)
            check("blocked requests never reach upstream", len(seen) == hits, str(seen[hits:]))
            time.sleep(0.3)
            log = Path(raw, "access.log")
            entries = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            check("access log is JSON with uri/status/duration",
                  entries and all({"status", "duration", "size"} <= e.keys() and "uri" in e["request"] for e in entries))
            check("key never logged", "sk-test-secret" not in (log.read_text() if log.exists() else ""))
        finally:
            proc.terminate()
            out = proc.communicate(timeout=10)[0].decode(errors="replace")
            upstream.shutdown()
            if failures:
                print(out[-2000:], file=sys.stderr)


def test_gateway_env_from_openab_env():
    """切換時 key 從 env/openab.env 搬到只給 Caddy 讀的 /etc/work-agent/gateway.env（#24 決定 6）。"""
    installer = ROOT / "scripts/install-host-network.sh"

    def render(text):
        with tempfile.TemporaryDirectory() as raw:
            src = Path(raw, "openab.env")
            src.write_text(text)
            return subprocess.run(["bash", str(installer), "gateway-env", str(src)],
                                  capture_output=True, text=True)

    got = render("SLACK_BOT_TOKEN=xoxb-1\nCOMPANY_GATEWAY_BASE_URL=https://gw.example.com/openai/v1/\n"
                 "COMPANY_GATEWAY_API_KEY=sk-abc_123.DEF\n")
    check("gateway-env splits base URL", got.returncode == 0 and got.stdout.splitlines() == [
        "COMPANY_GATEWAY_ORIGIN=https://gw.example.com",
        "COMPANY_GATEWAY_PATH_PREFIX=/openai/v1",
        "COMPANY_GATEWAY_API_KEY=sk-abc_123.DEF",
    ], got.stdout + got.stderr)
    check("gateway-env drops other secrets", "xoxb" not in got.stdout)
    got = render("COMPANY_GATEWAY_BASE_URL=https://gw.example.com\nCOMPANY_GATEWAY_API_KEY=k1\n")
    check("gateway-env without path prefix", got.returncode == 0
          and "COMPANY_GATEWAY_PATH_PREFIX=" in got.stdout.splitlines(), got.stdout + got.stderr)
    for bad, why in (("COMPANY_GATEWAY_BASE_URL=https://gw.example.com\n", "missing key"),
                     ("COMPANY_GATEWAY_BASE_URL=http://gw.example.com\nCOMPANY_GATEWAY_API_KEY=k\n", "plain http"),
                     ("COMPANY_GATEWAY_BASE_URL=https://gw.example.com\nCOMPANY_GATEWAY_API_KEY=\"k\"\n", "quoted key"),
                     ("COMPANY_GATEWAY_BASE_URL=https://gw.example.com\nCOMPANY_GATEWAY_API_KEY=a$b\n", "shell chars")):
        got = render(bad)
        check(f"gateway-env rejects {why}", got.returncode != 0 and not got.stdout, got.stdout)


def test_install_wiring():
    versions = load_env(ROOT / "config/versions.env")
    check("Caddy version is exact", re.fullmatch(r"\d+\.\d+\.\d+", versions.get("CADDY_VERSION", "")))
    for arch in ("AMD64", "ARM64"):
        check(f"Caddy {arch} SHA-512 pinned",
              re.fullmatch(r"[0-9a-f]{128}", versions.get(f"CADDY_LINUX_{arch}_SHA512", "")))
    installer = (ROOT / "scripts/install-host-network.sh").read_text()
    fw_unit = (ROOT / "config/systemd/work-agent-firewall.service").read_text()
    proxy_unit = (ROOT / "config/systemd/work-agent-proxy.service").read_text()
    check("firewall unit is oneshot", "Type=oneshot" in fw_unit and "RemainAfterExit=yes" in fw_unit)
    check("firewall unit runs before docker", "Before=docker.service" in fw_unit and "PartOf=docker.service" in fw_unit)
    check("firewall unit has no ExecStop", not re.search(r"^ExecStop", fw_unit, re.M))
    check("firewall unit runs installed script",
          "ExecStart=/usr/local/sbin/work-agent-firewall apply" in fw_unit
          and "/usr/local/sbin/work-agent-firewall" in installer)
    check("proxy unit after docker", re.search(r"^After=.*docker\.service", proxy_unit, re.M))
    check("proxy unit runs pinned caddy symlink",
          "/usr/local/bin/work-agent-caddy run --config /etc/work-agent/Caddyfile" in proxy_unit
          and "ln -sfn \"$dest/caddy\" /usr/local/bin/work-agent-caddy" in installer)
    check("proxy key only via EnvironmentFile",
          "EnvironmentFile=/etc/work-agent/gateway.env" in proxy_unit and "API_KEY" not in proxy_unit)
    check("proxy runs unprivileged", "DynamicUser=yes" in proxy_unit and "LogsDirectory=work-agent-proxy" in proxy_unit)
    check("installer verifies checksum", "sha512sum -c" in installer)
    check("network created with icc disabled", "com.docker.network.bridge.enable_icc=false" in installer)
    check("installer never touches br_netfilter", "modprobe" not in installer)


def test_compose_matches_network_env():
    """compose 接上 work-agent-net（#31）之後，寫死的值必須等於 host-network.env。"""
    compose = (ROOT / "compose.yaml").read_text()
    if NET["WORK_AGENT_NETWORK"] not in compose:
        return
    check("compose network is external", re.search(r"external:\s*true", compose))
    check("bot pinned to WORK_AGENT_BOT_IP", f"ipv4_address: {NET['WORK_AGENT_BOT_IP']}" in compose)
    check("compose does not enable IPv6", "enable_ipv6: true" not in compose)
    proxy = f"http://{NET['WORK_AGENT_GATEWAY_IP']}:{NET['WORK_AGENT_PROXY_PORT']}"
    check("bot gateway URL points at the proxy", proxy in compose or proxy in (ROOT / "env/openab.env.example").read_text())
    for dns in NET["WORK_AGENT_SANDBOX_DNS"].split(","):
        check(f"sandbox DNS {dns}", dns in compose)


def main():
    for test in (test_install_wiring, test_compose_matches_network_env, test_gateway_env_from_openab_env, test_caddyfile_contract, test_network_env_consistent, test_caddy_routes_live, test_policy_after_apply, test_apply_is_idempotent_and_first,
                 test_check_and_remove, test_applies_before_docker_starts,
                 test_warns_without_br_netfilter, test_never_unloads_br_netfilter):
        test()
    if failures:
        print("host-network.py FAILED", file=sys.stderr)
        for failure in failures:
            print(f" - {failure}", file=sys.stderr)
        sys.exit(1)
    print("host-network.py: ok")


if __name__ == "__main__":
    main()
