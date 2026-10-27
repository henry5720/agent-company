#!/usr/bin/env bash
# bot／prototype sandbox 的 host 防火牆（spec #21 §2；#25）。要 root；systemd 的
# work-agent-firewall.service 開機時跑 `apply`，維護者手動跑 `check` 驗、`remove` 回滾。
#
#   host-firewall.sh apply    建立或修正規則；已經正確時什麼都不改（冪等）
#   host-firewall.sh check    規則完整且兩個 jump 都在第一位才 exit 0
#   host-firewall.sh remove   拿掉本腳本加的 jump 與鏈（回滾用）
#
# 位址全部來自 config/host-network.env（裝到 host 後是 /etc/work-agent/host-network.env，
# 用 WORK_AGENT_NETWORK_ENV 指定）。
#
# 前提：
# - network 由 install-host-network.sh 以 com.docker.network.bridge.enable_icc=false 建立，
#   同一個 bridge 上 container 之間預設不通；這裡只開一個洞（bot → sandbox runner port）。
# - Docker 用 iptables firewall backend（預設），FORWARD 會 jump 到 DOCKER-USER。
#   systemd unit 排在 docker.service 之前，開機時 DOCKER-USER 可能還不存在：這裡先建好
#   （Docker 啟動時沿用既有的 DOCKER-USER、不清內容，只補上 FORWARD 的 jump），
#   這樣 `restart: unless-stopped` 的 sandbox 一被拉起來就已經受管，沒有空窗。
#   `check` 會確認 FORWARD 真的 jump 到 DOCKER-USER，抓得到 Docker 改用 nftables backend
#   這種「規則在、但沒人走過」的情況。ufw 不會碰我們自己的鏈。
# - 規則都用 `-i <bridge>` 限定，host 上其他 Docker network 與 ufw 的行為不變。
#
# spec 的「2 條 DOCKER-USER＋1 條 INPUT」在這裡長成兩條自己的鏈，各從 DOCKER-USER／INPUT
# 的第一位 jump 進來：
#   WORK-AGENT-FORWARD（DOCKER-USER）
#     ① bot → 同一個 bridge 的 runner port：ACCEPT（其他 port 交給 Docker 的 icc=false 丟掉）
#     ② bot 以外的位址（= sandbox）→ 私有／保留位址段：DROP（每段一條規則）
#     已建立連線的回包先 RETURN，讓 sandbox 回 bot 的 runner 回應不被 ② 擋掉。
#   WORK-AGENT-INPUT（INPUT）
#     ③ 從 bridge 進 host 的只放 bot → gateway IP:proxy port，其餘 DROP
#     （host 主動連 container 的回包，例如 docker-proxy 轉 127.0.0.1 port，照樣放行）。
# bot 本身往外網、VPC DNS 不受限：bot 要連 Slack、R2，而 Docker 內建 DNS 從 bot 的
# namespace 轉送到 VPC resolver。
#
# 刻意不做的事：
# - 不 modprobe、更不 `modprobe -r br_netfilter`。卸載會連 `bridge` 一起拿掉，實測讓正式
#   bot 斷線約 7.5 小時。br_netfilter 由 Docker 建 icc=false network 時載入；沒載入就警告。
# - 不加 ExecStop 清規則：Docker 重啟期間規則留著才是 fail closed。
# - 不用 ufw before.rules：Docker 的 FORWARD 流量不經過 ufw 的鏈。
# - 不處理 IPv6：network 不開 IPv6，container 沒有 v6 路由（compose 不得設 enable_ipv6）。
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# repo 內跑讀 repo 的正本；裝到 /usr/local/sbin 後讀 /etc 的副本。
NETWORK_ENV=${WORK_AGENT_NETWORK_ENV:-$ROOT/config/host-network.env}
[[ -n ${WORK_AGENT_NETWORK_ENV:-} || -r $NETWORK_ENV ]] || NETWORK_ENV=/etc/work-agent/host-network.env
BRIDGE_NF_PATH=${WORK_AGENT_BRIDGE_NF_PATH:-/proc/sys/net/bridge/bridge-nf-call-iptables}

FWD_CHAIN=WORK-AGENT-FORWARD
IN_CHAIN=WORK-AGENT-INPUT

# sandbox 不得連到的目的地。169.254.0.0/16 涵蓋 EC2 IMDS（169.254.169.254）與 Amazon DNS；
# 172.16.0.0/12 涵蓋 VPC、bot 以外的 Docker network；100.64.0.0/10 是 tailnet；
# 10.0.0.0/8 涵蓋本 network 自己（含 bot 與另一格 sandbox）。
BLOCKED_RANGES=(
  10.0.0.0/8
  172.16.0.0/12
  192.168.0.0/16
  100.64.0.0/10
  169.254.0.0/16
  0.0.0.0/8
  224.0.0.0/4
  240.0.0.0/4
)

die() { printf 'host-firewall: %s\n' "$*" >&2; exit 1; }
ipt() { iptables -w "$@"; }

load_network_env() {
  [[ -r $NETWORK_ENV ]] || die "cannot read $NETWORK_ENV"
  local key value
  while IFS='=' read -r key value; do
    [[ $key =~ ^WORK_AGENT_[A-Z_]+$ ]] || continue
    printf -v "$key" '%s' "$value"
  done < "$NETWORK_ENV"
  local ipv4='^([0-9]{1,3}\.){3}[0-9]{1,3}$'
  [[ ${WORK_AGENT_BRIDGE:-} =~ ^[A-Za-z0-9_-]{1,15}$ ]] || die "WORK_AGENT_BRIDGE missing or invalid"
  [[ ${WORK_AGENT_BOT_IP:-} =~ $ipv4 ]] || die "WORK_AGENT_BOT_IP missing or invalid"
  [[ ${WORK_AGENT_GATEWAY_IP:-} =~ $ipv4 ]] || die "WORK_AGENT_GATEWAY_IP missing or invalid"
  [[ ${WORK_AGENT_PROXY_PORT:-} =~ ^[0-9]+$ ]] || die "WORK_AGENT_PROXY_PORT missing or invalid"
  [[ ${WORK_AGENT_RUNNER_PORT:-} =~ ^[0-9]+$ ]] || die "WORK_AGENT_RUNNER_PORT missing or invalid"
}

# 每條規則一行，欄位用空白分隔（值本身都沒有空白）。
forward_rules() {
  local br=$WORK_AGENT_BRIDGE bot=$WORK_AGENT_BOT_IP/32 range
  echo "-m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
  echo "-s $bot -i $br -o $br -p tcp -m tcp --dport $WORK_AGENT_RUNNER_PORT -j ACCEPT"
  for range in "${BLOCKED_RANGES[@]}"; do
    echo "! -s $bot -d $range -i $br -j DROP"
  done
}

input_rules() {
  echo "-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
  echo "-s $WORK_AGENT_BOT_IP/32 -d $WORK_AGENT_GATEWAY_IP/32 -p tcp -m tcp --dport $WORK_AGENT_PROXY_PORT -j ACCEPT"
  echo "-j DROP"
}

chain_exists() { ipt -S "$1" >/dev/null 2>&1; }

# 鏈內容正確 = 規則數相同且每一條 `-C` 都在。用 `-C` 而不是比對 `-S` 文字，避免
# iptables 版本間輸出格式差異造成每次都判定漂移。
chain_ok() {
  local chain=$1 rules_fn=$2 rule count
  chain_exists "$chain" || return 1
  count=$(ipt -S "$chain" | grep -c '^-A ' || true)
  [[ $count -eq $("$rules_fn" | wc -l) ]] || return 1
  while read -r rule; do
    # shellcheck disable=SC2086 # rule 是刻意拆成多個參數
    ipt -C "$chain" $rule 2>/dev/null || return 1
  done < <("$rules_fn")
}

sync_chain() {
  local chain=$1 rules_fn=$2 rule
  chain_ok "$chain" "$rules_fn" && return 0
  chain_exists "$chain" || ipt -N "$chain"
  # 只有內容真的不對時才 flush；重填之間有毫秒級空窗，開機時鏈本來就是空的。
  ipt -F "$chain"
  while read -r rule; do
    # shellcheck disable=SC2086
    ipt -A "$chain" $rule
  done < <("$rules_fn")
}

# parent 鏈裡這條 jump 的規則編號（可能有多個）。
jump_numbers() {
  local parent=$1 spec=$2
  ipt -S "$parent" | awk -v want="-A $parent $spec" '/^-A /{n++; if ($0 == want) print n}'
}

jump_ok() {
  local numbers
  numbers=$(jump_numbers "$1" "$2")
  [[ $numbers == 1 ]]
}

ensure_jump_first() {
  local parent=$1 spec=$2 n
  jump_ok "$parent" "$spec" && return 0
  # 先插到第一位再刪多餘的，過程中規則一直有效。
  # shellcheck disable=SC2086
  ipt -I "$parent" 1 $spec
  for n in $(jump_numbers "$parent" "$spec" | sort -rn); do
    [[ $n -eq 1 ]] || ipt -D "$parent" "$n"
  done
}

remove_jump() {
  local parent=$1 spec=$2 n
  chain_exists "$parent" || return 0
  for n in $(jump_numbers "$parent" "$spec" | sort -rn); do
    ipt -D "$parent" "$n"
  done
}

warn_br_netfilter() {
  if [[ $(cat "$BRIDGE_NF_PATH" 2>/dev/null || true) != 1 ]]; then
    printf 'host-firewall: warning: br_netfilter is not active (%s != 1); same-bridge traffic bypasses iptables until Docker loads it. Do NOT modprobe -r br_netfilter.\n' \
      "$BRIDGE_NF_PATH" >&2
  fi
}

main() {
  local action=${1:-}
  load_network_env
  local fwd_jump="-i $WORK_AGENT_BRIDGE -j $FWD_CHAIN"
  local in_jump="-i $WORK_AGENT_BRIDGE -j $IN_CHAIN"
  case $action in
    apply)
      chain_exists DOCKER-USER || ipt -N DOCKER-USER
      sync_chain "$FWD_CHAIN" forward_rules
      sync_chain "$IN_CHAIN" input_rules
      ensure_jump_first DOCKER-USER "$fwd_jump"
      ensure_jump_first INPUT "$in_jump"
      warn_br_netfilter
      ;;
    check)
      local ok=0
      chain_ok "$FWD_CHAIN" forward_rules || { echo "$FWD_CHAIN missing or drifted" >&2; ok=1; }
      chain_ok "$IN_CHAIN" input_rules || { echo "$IN_CHAIN missing or drifted" >&2; ok=1; }
      # shellcheck disable=SC2015 # A 或 B 任一失敗都要記錯，正是想要的語意
      chain_exists DOCKER-USER && jump_ok DOCKER-USER "$fwd_jump" \
        || { echo "DOCKER-USER does not start with: $fwd_jump" >&2; ok=1; }
      ipt -S FORWARD | grep -qx -- '-A FORWARD -j DOCKER-USER' \
        || { echo "FORWARD does not jump to DOCKER-USER (docker not running, or not the iptables backend?)" >&2; ok=1; }
      jump_ok INPUT "$in_jump" || { echo "INPUT does not start with: $in_jump" >&2; ok=1; }
      warn_br_netfilter
      return "$ok"
      ;;
    remove)
      remove_jump DOCKER-USER "$fwd_jump"
      remove_jump INPUT "$in_jump"
      local chain
      for chain in "$FWD_CHAIN" "$IN_CHAIN"; do
        if chain_exists "$chain"; then ipt -F "$chain"; ipt -X "$chain"; fi
      done
      ;;
    *)
      die "usage: host-firewall.sh apply|check|remove"
      ;;
  esac
}

main "$@"
