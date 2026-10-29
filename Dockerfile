# Base image 與所有版本的正本是 config/versions.env；Compose 直接傳入固定值。
ARG OPENAB_IMAGE
FROM ${OPENAB_IMAGE}

ARG CODEX_VERSION
ARG CODEX_ACP_VERSION
ARG CODEGRAPH_VERSION
ARG SKILLSHARE_VERSION
ARG PLAYWRIGHT_CLI_VERSION
ARG SKILLSHARE_LINUX_AMD64_SHA256
ARG SKILLSHARE_LINUX_ARM64_SHA256

# Bind mounts carry host ownership, so the container user must share the host
# user's uid/gid. Only /home/node and /usr/local/bin/openab are owned by the
# image's node user, so remapping is cheap.
ARG HOST_UID=1000
ARG HOST_GID=1000

USER root
# python3：slack-list、company-image、slack-thread-artifact 都只用 stdlib。
# git、curl 上游已有，列出來是因為讀 snapshot 與 clone 副本都靠 git。
# 上游 image 的 gh 照官方保留；container 裡沒有 GitHub credential，它什麼都推不出去。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git python3 \
    && rm -rf /var/lib/apt/lists/*
RUN npm i -g "@colbymchenry/codegraph@${CODEGRAPH_VERSION}"
# playwright-cli 只 attach 到 chrome sidecar（127.0.0.1:9222），不下載瀏覽器、不裝系統依賴。
RUN npm i -g "@playwright/cli@${PLAYWRIGHT_CLI_VERSION}" \
    && test "$(node -p 'require(process.argv[1]).version' "$(npm root -g)/@playwright/cli/package.json")" = "$PLAYWRIGHT_CLI_VERSION" \
    && command -v playwright-cli

# codex 與 codex-acp 一起裝在獨立 prefix，不用上游 global 裝的那兩個（版本跟 base digest 走）。
# codex-acp 用 createRequire 解析自己底下的 @openai/codex，所以真正跑哪版 codex 由
# config/openab.toml 的 CODEX_PATH 決定，指到這裡的 node_modules/.bin/codex。
# build 時驗：PATH 上的 codex-acp／codex 都解析進這個 prefix、兩者 exact version 相符。
RUN set -eu; \
    CODEX_PREFIX="/opt/codex-${CODEX_VERSION}"; \
    npm uninstall -g @agentclientprotocol/codex-acp @openai/codex; \
    npm install --prefix "$CODEX_PREFIX" --fetch-retries=3 \
      "@openai/codex@${CODEX_VERSION}" "@agentclientprotocol/codex-acp@${CODEX_ACP_VERSION}"; \
    ln -sf "$CODEX_PREFIX/node_modules/.bin/codex-acp" /usr/local/bin/codex-acp; \
    ln -sf "$CODEX_PREFIX/node_modules/.bin/codex" /usr/local/bin/codex; \
    for bin in codex-acp codex; do \
      resolved="$(readlink -f "$(command -v "$bin")")"; \
      case "$resolved" in \
        "$CODEX_PREFIX"/node_modules/*) ;; \
        *) echo "$bin resolved outside $CODEX_PREFIX: $resolved" >&2; exit 1 ;; \
      esac; \
    done; \
    test "$("$CODEX_PREFIX/node_modules/.bin/codex" --version)" = "codex-cli ${CODEX_VERSION}"; \
    test "$(node -p 'require(process.argv[1]).version' "$CODEX_PREFIX/node_modules/@agentclientprotocol/codex-acp/package.json")" = "$CODEX_ACP_VERSION"; \
    chmod -R a+rX,go-w "$CODEX_PREFIX"

# skillshare：跟維護者機器同一個工具，把 agent-config 的 skills 與 MCP 裝進 Codex（agent-sync 呼叫）。
# 從 GitHub release 下載固定版本，SHA-256 不符就中止。
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) arch=amd64; sum="$SKILLSHARE_LINUX_AMD64_SHA256" ;; \
      arm64) arch=arm64; sum="$SKILLSHARE_LINUX_ARM64_SHA256" ;; \
      *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    tarball="skillshare_${SKILLSHARE_VERSION}_linux_${arch}.tar.gz"; \
    curl -fsSL -o "/tmp/$tarball" "https://github.com/runkids/skillshare/releases/download/v${SKILLSHARE_VERSION}/$tarball"; \
    echo "$sum  /tmp/$tarball" | sha256sum -c -; \
    tar -xzf "/tmp/$tarball" -C /usr/local/bin skillshare; \
    rm "/tmp/$tarball"; \
    chown root:root /usr/local/bin/skillshare; chmod 0755 /usr/local/bin/skillshare; \
    skillshare --version | grep -qF "v${SKILLSHARE_VERSION}"

# 生圖／改圖的唯一入口。它只用 Python 3 stdlib。
# root 擁有、0755：agent 可以執行，不能改寫。
COPY agents/bin/company-image /usr/local/bin/company-image
COPY agents/bin/slack-thread-artifact /usr/local/bin/slack-thread-artifact
COPY agents/bin/agent-sync /usr/local/bin/agent-sync
RUN chmod 0755 /usr/local/bin/company-image /usr/local/bin/agent-sync \
    && chown root:root /usr/local/bin/agent-sync \
    && chown root:root /usr/local/bin/company-image \
    && chmod 0755 /usr/local/bin/slack-thread-artifact \
    && chown root:root /usr/local/bin/slack-thread-artifact \
    && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'

# /home/node 是 named volume `home`。第一次掛上空 volume 時 Docker 會把 image 內的 /home/node
# 連同 ownership 複製進去，所以唯讀 mount 的父目錄先建好，讓下面的 chown -R 接手；
# 否則 Docker 會以 root:root 建出這些目錄，node 寫不進 CODEX_HOME。之後再改這裡的內容，
# 已存在的 volume 不會跟著更新。
RUN mkdir -p /home/node/.codex /home/node/.agents /home/node/snapshots /home/node/.local/bin \
      /home/node/.config/skillshare \
    && printf 'prefix=/home/node/.local\n' > /home/node/.npmrc
# agent 不是 root，/usr/local 寫不進去；`npm i -g`（例如 pnpm）裝到 home，重啟後還在。
ENV PATH=/home/node/.local/bin:$PATH

RUN if [ "$HOST_GID" != "1000" ]; then groupmod -g "$HOST_GID" node; fi \
    && if [ "$HOST_UID" != "1000" ]; then usermod -u "$HOST_UID" -g "$HOST_GID" node; fi \
    && chown -R "$HOST_UID:$HOST_GID" /home/node /usr/local/bin/openab
USER node
RUN company-image --help >/dev/null \
    && python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' /usr/local/bin/agent-sync \
    && slack-thread-artifact --help >/dev/null \
    && ! command -v opencode && ! command -v claude
