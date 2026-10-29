# agent-company

在一台 Linux deployment host 上部署公司 Slack 上共用的 coding agent。OpenAB 接 Slack Socket Mode，agent 用定期同步的唯讀 repo snapshot 回答產品問題、讀寫 Slack 待辦；需要改 code 時 clone 一份到自己的 home 修改、安裝、跑測試，再把 `.patch` 傳回 Slack thread。它送不回 GitHub，container 裡沒有任何 GitHub credential。

形狀照 OpenAB 官方的 Codex 部署：一個 container、可寫的 home、直連公司 gateway。理由見 [ADR 0012](docs/adr/0012-follow-the-official-openab-codex-deployment.md)。

## 架構

```text
Slack
  -> OpenAB（單一 container，最多 10 個 session，閒置 4 小時回收）
    -> codex-acp（mode agent-full-access）
      -> 公司 gateway 的 Responses API（模型）
      -> company-image -> 公司 gateway 的 Responses image_generation（生圖，PNG 寫到 drafts）
      -> slack-thread-artifact -> 原 Slack thread（PNG／Markdown／HTML／patch）
      -> /home/node                  home，named volume `home`，可寫、持久
         -> clone 出來的工作副本、自己裝的套件與工具、Codex session、drafts
      -> /home/node/snapshots/*      repo snapshots，唯讀，單一 mount
      -> /home/node/snapshots/.index  CodeGraph 索引，可寫
      -> ~/.agents/skills、~/.codex 的 MCP 與 AGENTS.md   agent-sync 用 skillshare 從 agent-config 裝，跟維護者機器同一套
  -> chrome sidecar（共用 localhost）  headless Chrome，127.0.0.1:9222；playwright-cli attach 操作與截圖，chrome-devtools MCP 查問題
      -> Cloudflare R2 filestore（incoming attachments）

deployment host
  -> crontab 每小時 fetch/reset snapshots，再用 runtime image 重建索引，最後在 bot 裡跑 agent-sync
  -> crontab 每小時清掉上傳失敗、超過 24 小時的 artifact
  -> 專用 GitHub SSH key，只存在 host
```

Container 沒有 GitHub token、SSH key 或 Docker socket。image 裡有 `gh`（官方 image 自帶），但沒有 token，什麼都推不出去。Runtime image 由固定 multi-arch digest 的 OpenAB Codex image 建置，加上固定版本的 Codex CLI、codex-acp、CodeGraph 與 Python 3；版本正本在 [`config/versions.env`](config/versions.env)。

接受的取捨，照實寫：

- Slack bot token、公司 gateway key 與 agent 執行的 repo code（含第三方套件的 install script）在同一個 container。沒有 broker、proxy 或 token 隔離。
- home 沒有自動清理，clone 與套件快取會累積。
- dev server 只在 container 內看得到，要給人看就截圖。預覽網址之後另做。

## 輸入與輸出

| 方向 | 支援 | 不支援 |
|---|---|---|
| 輸入 | text、image；PDF、DOCX、XLSX、PPTX 等附件由 agent 自己下載轉文字 | audio（STT 未啟用時）、需要 OCR 的掃描檔 |
| 輸出 | Slack 訊息、PNG、`.patch`、Markdown、明確要求時的 self-contained HTML、截圖 | push、PR、GitHub issue |

產物一律回原本的 Slack thread。這是同一個 container 內 OpenAB、agent 與 CLI 之間的信任約定，不是安全保證，細節見 [`docs/system-design.md`](docs/system-design.md#回原-thread靠的是什麼)。

## Cloudflare R2 filestore

[`config/openab.toml`](config/openab.toml) 的 `[filestore]` 是設定正本（bucket `work-agent-attachments`，沿用舊名，因為它是外部資源）。access key 與 secret key 不進 Git，只放在 deployment host 未追蹤的 `env/openab.env`：

```text
R2_ACCESS_KEY_ID=<Cloudflare R2 access key>
R2_SECRET_ACCESS_KEY=<Cloudflare R2 secret key>
```

R2 bucket 必須手動設定 `incoming/` prefix 的 **1 天 lifecycle expiration**；Slack app 必須有 `files:read`，改 scope 後要 **Reinstall to Workspace**。`preflight.sh` 不呼叫 Cloudflare 或 Slack API，它會拒絕缺少／placeholder 的 R2 secret 與錯誤 endpoint，再要求維護者在互動式 terminal 逐項確認這兩件事。`deploy.sh` 強制經過這個 gate，noninteractive deploy 會中止。

## Repo 內容

- `CONTEXT.md`：角色與領域詞彙。
- `CLAUDE.md`：給維護這個 deployment repo 的 agent；不會 mount 進 container。
- `agents/AGENTS.md`：bot 專屬規則，只寫跟維護者機器不同的地方。agent-sync 把它接在維護者個人規則（agent-config 的 `instructions/agents-md/AGENTS.md`）後面，寫成 container 內的 `~/.codex/AGENTS.md`。
- `docs/system-design.md`：系統設計、信任邊界、流程與驗收情境。
- `docs/runbook.md`：首次安裝、Slack 設定、日常操作與人工 release gate。
- `docs/adr/`：不容易從設定本身看懂的決策理由。
- `.env.example`：snapshot root 與 container 使用者 uid/gid；複製成 root `.env`。
- `env/openab.env.example`：runtime secret 範本；複製成未追蹤的 `env/openab.env`。
- `config/versions.env`：所有版本與 OpenAB image digest 的正本。
- `Dockerfile`：在固定 OpenAB Codex image 上裝固定版 Codex、codex-acp、CodeGraph 與兩支受限 CLI，再把 container 使用者的 uid 對齊 host。
- `compose.yaml`：agent container（home volume 與唯讀 mount）加上 headless Chrome sidecar。
- `config/openab.toml`：Slack 存取、session pool、R2 filestore 與 `codex-acp` agent 設定。
- `config/codex/config.toml`：Codex provider 與模型的範本；`scripts/deploy.sh` 填入 `COMPANY_GATEWAY_BASE_URL` 後寫到 `runtime/codex/config.toml`，唯讀 mount 成 `/etc/codex/config.toml`，由 agent-sync 換進 `~/.codex/config.toml`。
- `config/skillshare/config.yaml`：bot 裡的 skillshare 設定，source 是 snapshot 裡的 agent-config。
- `agents/bin/agent-sync`：OpenAB `pre_boot` 與每小時同步後執行；對齊 provider、規則，再跑 `skillshare sync -g` 與 `skillshare sync mcp -g`。
- `config/codex/catalog.json`：Codex 的 model catalog，升級 Codex 時要重產。
- `config/repos.conf`：snapshot 清單的正本，新增 repo 只改這裡。
- `config/slack-home.json`：使用者在 Slack Home 看到的功能首頁。
- `agents/bin/company-image`：生圖／改圖的唯一入口。
- `agents/bin/slack-thread-artifact`：把 drafts 的 PNG／Markdown／HTML／patch 傳回原 Slack thread。
- `scripts/preflight.sh`：部署前檢查 secrets、版本、Codex 設定與 snapshot 狀態。
- `scripts/deploy.sh`：產生 Codex 設定、build、啟動，驗 container 內的版本與權限，最後印出人工 release gate。
- `scripts/update-snapshots.sh`：host 端 clone/fetch/reset，並重建 CodeGraph 索引。
- `scripts/install-sync-cron.sh`：寫入兩個每小時的 crontab entry（snapshot 同步、artifact cleanup）並跑第一次同步。
- `scripts/cleanup-artifacts.sh`：cron 呼叫的那一支，在 container 內執行 `slack-list cleanup`。
- `scripts/publish-slack-home.sh`：對 workspace 成員逐一發布 Slack Home view。
- `tests/static.sh`：靜態檢查，並跑三支 mocked 測試（`image-runtime.py`、`slack-thread-artifact.py`、`artifact-path-safety.py`）。

AI agent 修改前從 [`CLAUDE.md`](CLAUDE.md) 的閱讀順序開始。

## 驗證

```bash
./tests/static.sh
bash -n scripts/*.sh tests/*.sh
docker compose config --quiet
```

`tests/static.sh` 只做靜態檢查，三支 mocked 測試都不呼叫 Slack 或公司 gateway。沒有 Docker 的機器上它會改用本機解析 `compose.yaml`，並在最後印出「LIMITED VERIFICATION」—— 那不是 smoke test 通過。

`scripts/deploy.sh` 只多驗 container 內的版本、檔案與讀寫權限。真的 Slack 往返、生圖、模型推論打到 gateway、coding 流程、STT 與 cleanup 排程是否觸發，都在 [`docs/runbook.md`](docs/runbook.md#人工-release-gate) 的「人工 release gate」，**必須由人各跑一次**；`deploy.sh` 退出 0 不代表那些過了，它結束時會把清單再印一次。

Local 與 deployment host 共用同一套設定。目標機器填好 secrets 並完成首次 snapshot sync 後：

```bash
./scripts/preflight.sh
./scripts/deploy.sh
```

完整步驟見 [`docs/runbook.md`](docs/runbook.md)。
