# Base image 與所有版本的正本是 config/versions.env；Compose 直接傳入固定值。
ARG OPENAB_IMAGE
FROM ${OPENAB_IMAGE}

ARG CODEX_VERSION
ARG CODEX_ACP_VERSION
ARG CODEGRAPH_VERSION

# Bind mounts carry host ownership, so the container user must share the host
# user's uid/gid. Only /home/node and /usr/local/bin/openab are owned by the
# image's node user, so remapping is cheap.
ARG HOST_UID=1000
ARG HOST_GID=1000

USER root
# 上游 codex image 有裝 gh；共用 bot 不給任何 GitHub 寫入（spec #21 §1、§3），連同 apt source
# 一起拿掉，之後的 apt-get 也裝不回來。
RUN apt-get purge -y gh \
    && rm -f /etc/apt/sources.list.d/github-cli.list /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && ! command -v gh
# python3：只給 slack-list、company-image、slack-thread-artifact（都只用 stdlib）。
# git、curl 上游已有，列出來是因為唯讀 snapshot 偵察與 thread workspace 都靠 git。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git python3 \
    && rm -rf /var/lib/apt/lists/*
RUN npm i -g "@colbymchenry/codegraph@${CODEGRAPH_VERSION}"

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

# 生圖／改圖的唯一入口。它只用 Python 3 stdlib。
# root 擁有、0755：agent 可以執行，不能改寫（rootfs 本來就是唯讀的，這是第二層）。
COPY agents/bin/company-image /usr/local/bin/company-image
COPY agents/bin/slack-thread-artifact /usr/local/bin/slack-thread-artifact
RUN chmod 0755 /usr/local/bin/company-image \
    && chown root:root /usr/local/bin/company-image \
    && chmod 0755 /usr/local/bin/slack-thread-artifact \
    && chown root:root /usr/local/bin/slack-thread-artifact \
    && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'

# CODEX_HOME（/home/node/.codex）是 named volume；空 volume 會沿用 mount point 在 image 內的
# ownership，所以先建好讓下面的 chown -R 接手，否則 fresh volume 是 root:root、codex 寫不進去。
# /home/node/.agents 是 skills 唯讀 mount 的父目錄。
RUN mkdir -p /home/node/.codex /home/node/.agents

RUN if [ "$HOST_GID" != "1000" ]; then groupmod -g "$HOST_GID" node; fi \
    && if [ "$HOST_UID" != "1000" ]; then usermod -u "$HOST_UID" -g "$HOST_GID" node; fi \
    && chown -R "$HOST_UID:$HOST_GID" /home/node /usr/local/bin/openab
USER node
RUN company-image --help >/dev/null \
    && slack-thread-artifact --help >/dev/null \
    && ! command -v gh && ! command -v opencode && ! command -v claude
