# prototype sandbox image（spec #21 §6、§7）。Base 與版本的正本是 config/versions.env；
# build 時以 build arg 傳入固定值，這裡不寫預設，漏傳就 build 失敗。
# Build context 是 sandbox/ 目錄。
ARG SANDBOX_NODE_IMAGE
FROM ${SANDBOX_NODE_IMAGE}

ARG PLAYWRIGHT_VERSION
# 附件轉換工具的版本；工具本身不預裝，job 執行時由 sandbox-attachment 現裝（#28）。
ARG OFFICEPARSER_VERSION

# root 只在 build 用：Playwright 的 --with-deps 要 apt。apt 不逐一釘版，跟著 base digest 走。
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN set -eu; \
    test -n "${PLAYWRIGHT_VERSION}"; \
    test -n "${OFFICEPARSER_VERSION}"; \
    npm install -g "playwright@${PLAYWRIGHT_VERSION}"; \
    test "$(playwright --version)" = "Version ${PLAYWRIGHT_VERSION}"; \
    playwright install --with-deps chromium; \
    rm -rf /var/lib/apt/lists/* /root/.npm; \
    chmod -R a+rX /ms-playwright
# corepack 依各 repo 的 packageManager 取 pnpm 或 npm；runtime 下載，不跳確認。
RUN corepack enable \
    && python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version'

COPY --chmod=0755 runner /usr/local/bin/sandbox-runner
COPY --chmod=0755 sandbox-screenshot /usr/local/bin/sandbox-screenshot
COPY --chmod=0755 sandbox-attachment /usr/local/bin/sandbox-attachment

# scratch 與 pnpm store 由 Compose 掛 named volume（每格一個 scratch、兩格共用 store）；
# 先建好目錄讓新 volume 繼承 node 的 ownership。
RUN install -d -o node -g node /home/node/scratch /home/node/.pnpm-store /home/node/.cache

# SANDBOX_SWEEP_ALL=1：這個 container 只有 runner 和它的 job，job 結束時連逃出
# session 的 process 一起收掉。
# GOMEMLIMIT：前端 vite.config 設 optimizeDeps.force，每次起 dev server 都重新預打包；
# esbuild（Go）會長到 1.9 GB、加上 vite 1.7 GB，4g 格子實測被 OOM kill。設成 1GiB
# 軟上限後同樣流程不再 OOM（#26）。V8 heap 不另設 --max-old-space-size：反覆改
# Tailwind 40 次 vite RSS 穩定在 1.5–1.6 GB。
ENV SANDBOX_SWEEP_ALL=1 \
    GOMEMLIMIT=1GiB \
    OFFICEPARSER_VERSION=${OFFICEPARSER_VERSION} \
    SANDBOX_RUNNER_PORT=8080 \
    SANDBOX_WORK_ROOT=/work \
    SANDBOX_OUT_ROOT=/out \
    SANDBOX_SCRATCH_ROOT=/home/node/scratch \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    npm_config_store_dir=/home/node/.pnpm-store

USER node
WORKDIR /home/node
EXPOSE 8080
CMD ["sandbox-runner"]
