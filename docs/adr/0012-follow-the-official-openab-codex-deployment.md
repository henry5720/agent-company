# 照 OpenAB 官方的 Codex 部署：一個 container、可寫的 home、直連 gateway

## Status

Accepted。取代 0006（唯讀 product-context bot）與 0007（OpenCode + OMO + Claude ACP specialist）；
0011 只對 OpenCode 有效，一併失效。spec #21 的 workspace agent 設計（host proxy、防火牆、prototype
sandbox、thread workspace helper）沒有採用，PR #39 不合併。

## Context

這個 repo 的初衷是把公司產品 repo 接給同事用，讓 AI 拿 repo 當 context 回答產品問題，而不是由人
代答或讓外部 agent 摸黑猜。後來要它也能動手改 code、跑起來看。

spec #21 為此設計了一整套邊界：gateway key 只放 host proxy、host 防火牆、跑 repo code 的 2 格
sandbox、每個 thread 一份工作副本。做完之後發現，這些限制大多不是 agent 在官方部署裡會碰到的，
維護成本卻比 bot 本身還高。

OpenAB 官方的 Codex 部署（`docs/codex.md`、README 的 Kubernetes Deployment）是：一個 container
同時跑 OpenAB 與 agent，home 是持久可寫的 PVC，mode 用 `agent-full-access`，邊界是 container 本身。

## Decision

- 照官方形狀：一個 container，`/home/node` 是 named volume `home`，`working_dir` 是 `/home/node`，
  mode 是 `agent-full-access`。rootfs 不再唯讀；agent 自己在 home 裡裝 pnpm 套件與工具、跑 dev server。
- 跟官方不同的是唯讀的 repo snapshot（`/home/node/snapshots`）。agent 要改 code 就自己
  clone 一份到 home。snapshot 唯讀是為了正確性（所有 thread 共用、host 每小時同步），不是安全邊界。
- Codex 直連公司 gateway。key 走 env（`env_key`），位址由 `scripts/deploy.sh` 填進
  `config/codex/config.toml` 的範本，因為 Codex 的 provider 設定不讀 env。
- container 裡沒有 GitHub credential。agent 改好的 code 用 `git format-patch` 產出 `.patch`
  上傳到 thread，由人在自己的電腦套用與 push。
- skills、MCP 與個人規則跟維護者機器同一套：container 裡裝固定版本的 skillshare，`agents/bin/agent-sync`
  在 OpenAB `pre_boot` 與每小時 snapshot 同步後從 agent-config 同步；`agents/AGENTS.md` 只寫 bot 專屬規則。
  不裝 chezmoi：它管的是機器本身（git 身分、provider、shell），在 bot 裡由這個 repo 負責。
- 加一個 headless Chrome sidecar（OpenAB `docs/sidecar.md` 的模式），跟 agent 共用 localhost，
  讓 agent-config 的 chrome-devtools MCP 設定（`--browser-url=http://127.0.0.1:9222`）原樣可用。
- 保留的自建部分：`company-image`、`slack-thread-artifact`（多接受 `.patch`）、snapshot 同步與
  artifact 清理兩個 cron。
- repo、compose project 與 container 改名為 `agent-company`。R2 bucket 與 GitHub SSH key 是外部資源，
  沿用 `work-agent-*` 舊名。

## Consequences

- Slack token、gateway key 與 agent 執行的 repo code（含第三方套件的 install script）在同一個
  container。這是照官方接受的取捨；這個 bot 只給公司內部使用。開放給公司外的人之前要重新決定。
- 沒有 host proxy，agent 讀得到 gateway key。
- dev server 只在 container 內看得到；要給人看就截圖。預覽網址（內網穿透）之後另做。
- home 沒有自動清理，clone 與套件快取會累積；volume 變大時再加清理。
- 切回 OpenCode 不靠保留的設定，而是 `git revert` 換 runtime 的那個 commit。
