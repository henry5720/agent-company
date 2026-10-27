#!/usr/bin/env bash
# 在 deployment host 裝 host proxy 與防火牆（spec #21 §2、§4；#25）。步驟與驗證見
# docs/runbook.md「Host proxy 與防火牆」。
#
#   sudo ./scripts/install-host-network.sh install
#       冪等：重跑會把 repo 的設定重新複製到 /etc 並重啟兩個 unit。
#   ./scripts/install-host-network.sh gateway-env <openab.env>
#       只印出要寫進 /etc/work-agent/gateway.env 的三行（不需要 root；測試也用這個）。
#
# install 做的事（全部從 repo 正本複製，host 上的副本不要手改）：
#   1. 下載 config/versions.env 釘的 Caddy，驗 SHA-512，裝到 /opt/caddy-<ver>/caddy，
#      /usr/local/bin/work-agent-caddy 指過去。
#   2. config/host-network.env、config/caddy/Caddyfile → /etc/work-agent/；
#      scripts/host-firewall.sh → /usr/local/sbin/work-agent-firewall。
#      裝成 root 擁有的副本，是因為 unit 以 root 跑；直接跑 repo 裡的檔案等於讓能寫 repo
#      的人取得 root。
#   3. /etc/work-agent/gateway.env 不存在時，從 env/openab.env 的 COMPANY_GATEWAY_BASE_URL、
#      COMPANY_GATEWAY_API_KEY 產生（root 0600）。已存在就不動。
#      這一步**不會**把 key 從 env/openab.env 拿掉：舊 bot 還在用，切換當晚才拿（§12）。
#   4. 建 Docker network work-agent-net（external，enable_icc=false，固定 subnet 與 bridge 名）。
#      已存在就比對設定，不符就停下，不自動重建（重建會把接在上面的 container 斷線）。
#   5. 裝兩個 systemd unit，enable 並（重新）啟動。
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ETC=/etc/work-agent

die() { printf 'install-host-network: %s\n' "$*" >&2; exit 1; }

# 只讀指定的 key，不 source（env 檔可能含其他 secret，也可能含 shell 字元）。
read_env_value() {
  local file=$1 want=$2 key value
  while IFS='=' read -r key value; do
    [[ $key == "$want" ]] && { printf '%s' "$value"; return 0; }
  done < "$file"
  return 1
}

gateway_env() {
  local src=$1 base key origin prefix
  [[ -r $src ]] || die "cannot read $src"
  base=$(read_env_value "$src" COMPANY_GATEWAY_BASE_URL) || die "COMPANY_GATEWAY_BASE_URL not in $src"
  key=$(read_env_value "$src" COMPANY_GATEWAY_API_KEY) || die "COMPANY_GATEWAY_API_KEY not in $src"
  [[ $base =~ ^(https://[A-Za-z0-9.-]+(:[0-9]+)?)(/[A-Za-z0-9._~/-]*)?$ ]] \
    || die "COMPANY_GATEWAY_BASE_URL must be https://host[:port][/path]"
  origin=${BASH_REMATCH[1]}
  prefix=${BASH_REMATCH[3]}
  while [[ $prefix == */ ]]; do prefix=${prefix%/}; done
  # systemd EnvironmentFile 會處理引號與反斜線；只收不需要跳脫的字元，避免 key 被悄悄改掉。
  [[ $key =~ ^[A-Za-z0-9._~+/=-]+$ ]] \
    || die "COMPANY_GATEWAY_API_KEY is empty or has characters EnvironmentFile would reinterpret"
  printf 'COMPANY_GATEWAY_ORIGIN=%s\n' "$origin"
  printf 'COMPANY_GATEWAY_PATH_PREFIX=%s\n' "$prefix"
  printf 'COMPANY_GATEWAY_API_KEY=%s\n' "$key"
}

install_caddy() {
  local version sha arch url dest tmp
  version=$(read_env_value "$ROOT/config/versions.env" CADDY_VERSION) || die "CADDY_VERSION missing"
  case $(uname -m) in
    x86_64) arch=amd64 ;;
    aarch64) arch=arm64 ;;
    *) die "unsupported arch $(uname -m)" ;;
  esac
  sha=$(read_env_value "$ROOT/config/versions.env" "CADDY_LINUX_${arch^^}_SHA512") || die "no checksum for $arch"
  dest=/opt/caddy-$version
  if [[ ! -x $dest/caddy ]]; then
    tmp=$(mktemp -d)
    url=https://github.com/caddyserver/caddy/releases/download/v$version/caddy_${version}_linux_${arch}.tar.gz
    curl -fsSL --proto '=https' -o "$tmp/caddy.tar.gz" "$url"
    printf '%s  %s\n' "$sha" "$tmp/caddy.tar.gz" | sha512sum -c --quiet - || die "checksum mismatch for $url"
    tar -xzf "$tmp/caddy.tar.gz" -C "$tmp" caddy
    install -d -m 0755 "$dest"
    install -m 0755 "$tmp/caddy" "$dest/caddy"
    rm -rf "$tmp"
  fi
  "$dest/caddy" version | grep -q "^v$version " || die "$dest/caddy is not v$version"
  ln -sfn "$dest/caddy" /usr/local/bin/work-agent-caddy
}

install_files() {
  install -d -m 0755 "$ETC"
  install -m 0644 "$ROOT/config/host-network.env" "$ETC/host-network.env"
  install -m 0644 "$ROOT/config/caddy/Caddyfile" "$ETC/Caddyfile"
  install -m 0755 "$ROOT/scripts/host-firewall.sh" /usr/local/sbin/work-agent-firewall
  if [[ -e $ETC/gateway.env ]]; then
    echo "keep existing $ETC/gateway.env (delete it to regenerate from env/openab.env)"
  else
    local content
    content=$(gateway_env "$ROOT/env/openab.env")
    (umask 077 && printf '%s\n' "$content" > "$ETC/gateway.env")
  fi
  chown root:root "$ETC/gateway.env"
  chmod 0600 "$ETC/gateway.env"
}

ensure_network() {
  local name subnet range gw bridge want got
  name=$(read_env_value "$ROOT/config/host-network.env" WORK_AGENT_NETWORK)
  subnet=$(read_env_value "$ROOT/config/host-network.env" WORK_AGENT_SUBNET)
  range=$(read_env_value "$ROOT/config/host-network.env" WORK_AGENT_IP_RANGE)
  gw=$(read_env_value "$ROOT/config/host-network.env" WORK_AGENT_GATEWAY_IP)
  bridge=$(read_env_value "$ROOT/config/host-network.env" WORK_AGENT_BRIDGE)
  want="$subnet $range $gw $bridge false"
  if ! docker network inspect "$name" >/dev/null 2>&1; then
    docker network create --driver bridge \
      --subnet "$subnet" --ip-range "$range" --gateway "$gw" \
      -o "com.docker.network.bridge.name=$bridge" \
      -o com.docker.network.bridge.enable_icc=false \
      --label work-agent.managed-by=scripts/install-host-network.sh \
      "$name" >/dev/null
  fi
  got=$(docker network inspect "$name" --format \
    '{{range .IPAM.Config}}{{.Subnet}} {{.IPRange}} {{.Gateway}}{{end}} {{index .Options "com.docker.network.bridge.name"}} {{index .Options "com.docker.network.bridge.enable_icc"}}')
  [[ $got == "$want" ]] || die "network $name is '$got', expected '$want'; fix by hand (recreating disconnects containers)"
}

install_units() {
  install -m 0644 "$ROOT/config/systemd/work-agent-firewall.service" /etc/systemd/system/
  install -m 0644 "$ROOT/config/systemd/work-agent-proxy.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable work-agent-firewall.service work-agent-proxy.service
  systemctl restart work-agent-firewall.service
  systemctl restart work-agent-proxy.service
}

case ${1:-} in
  gateway-env)
    [[ $# -eq 2 ]] || die "usage: install-host-network.sh gateway-env <openab.env>"
    gateway_env "$2"
    ;;
  install)
    [[ $EUID -eq 0 ]] || die "install needs root (sudo)"
    install_caddy
    install_files
    ensure_network
    install_units
    /usr/local/sbin/work-agent-firewall check
    systemctl --no-pager --lines=5 status work-agent-proxy.service
    ;;
  *)
    die "usage: install-host-network.sh install | gateway-env <openab.env>"
    ;;
esac
