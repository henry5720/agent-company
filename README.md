# work-agent-deploy

在一台 Linux deployment host 上部署共享的 Slack product-context bot。OpenAB 接 Slack Socket Mode，agent 只讀定期更新的 repo snapshot，只回答產品問題，並能查詢或建立指派給自己的 Slack 待辦；不能改 code、建立 GitHub issue 或 push。

## 架構

```text
Slack
  -> OpenAB (單一 container)
    -> opencode acp + oh-my-opencode-slim (最多 10 個 session，閒置 4 小時回收)
      -> 公司 OpenAI-compatible gateway (模型)
       -> company-image -> 公司 gateway 的 Responses image_generation (生圖，輸出 PNG 到 drafts)
       -> slack-thread-artifact -> 原 Slack thread（PNG／HTML）
       -> /home/node/code/* (read-only snapshots，單一 mount)
       -> /home/node/code/.index (CodeGraph 索引，唯一可寫的 repo 相關路徑)
       -> /home/node/drafts (artifact 暫存，可寫)
       -> Cloudflare R2 filestore (incoming attachments)
       -> parse-document -> Markdown（PDF／DOCX／XLSX／PPTX；ZIP 只列檔）
       -> /home/node/.claude/skills (agent-config/skills 的 read-only mount)

deployment host
  -> crontab 每小時 fetch/reset snapshots，再用 runtime image 重建索引
  -> 專用 GitHub SSH key，只存在 host
```

Container 沒有 GitHub token、SSH key、Docker socket或可寫的 repo checkout。Runtime image由固定 multi-arch digest的 OpenAB image建置，加上 Python 3、git、Docling、CodeGraph 與 OMO，全部固定版本，不跟浮動 tag更新。Docling 只預取 layout/table artifacts；不啟用掃描 PDF OCR，也不安裝 OCR、VLM、video、ASR 或 LibreOffice runtime。

只有一個 container：沒有獨立 broker 或 relay。Slack bot token 與公司 gateway key
共用這個 container，這是目前部署的取捨。

## Agent runtime 與 Claude specialist

OpenAB 固定使用 `opencode acp`；OMO 可依任務自行決定是否委派同一個 container 內、由 Docker image
固定安裝的 Claude Code ACP specialist。specialist 的 `acpAgents.claude-code` 執行固定版
`/usr/local/bin/claude-agent-acp`，不使用 runtime `npx` 下載。

不需要特殊 prefix。OMO Slim v2.2.15 使用內建 autonomous routing，依任務自行決定是否透過

`@claude-code` ACP wrapper 委派 Claude Code specialist。這是 LLM routing，非 deterministic，不保證每個複雜工作
都會交給 Claude Code。orchestrator 不直接啟動 ACP；wrapper 失敗時要明確回報委派失敗，不靜默
fallback 到本地處理。


所有 compose 指令可直接使用 `docker compose`；image digest 與 build args 已寫在
`compose.yaml`，並由 `config/versions.env` 記錄正本。

## Cloudflare R2 filestore

OpenAB config 固定 Cloudflare R2 filestore 設定：bucket
`work-agent-attachments`、region `auto`、prefix `incoming/`、presigned URL TTL 3600 秒，
以及單檔上限 **50 MiB**。endpoint 是
`https://99de68928da234ebcf0c9370443ad7ee.r2.cloudflarestorage.com`；只有 access key 與
secret key 不進 Git，由 `${R2_ACCESS_KEY_ID}`／`${R2_SECRET_ACCESS_KEY}` interpolation 取得。

部署到 deployment host 前，維護者必須手動在未追蹤的 `env/openab.env` 加入：

```text
R2_ACCESS_KEY_ID=<Cloudflare R2 access key>
R2_SECRET_ACCESS_KEY=<Cloudflare R2 secret key>
```

Compose 會把 `PARSE_DOCUMENT_ALLOWED_HOST` 固定提供給 runtime；它只作 URL host gate，不是
credential 或 API 驗證。Slack app 必須有 `files:read`，改 scope 後要 **Reinstall to Workspace**。
Cloudflare R2 bucket `work-agent-attachments` 必須手動設定 `incoming/` prefix 的 **1 天
lifecycle expiration**。`preflight.sh` 不會呼叫 Cloudflare 或 Slack API；它會先拒絕缺少／placeholder
R2 secret、錯誤 host，然後要求維護者在互動式 terminal 逐項確認 lifecycle 與 `files:read`。
`deploy.sh` 強制經過這個 gate，noninteractive deploy 會中止。

### 正式文件附件流程

PDF、DOCX、XLSX、PPTX 是正式支援的文件附件。OpenAB 會把 R2 `incoming/` 的 presigned URL
與 filename 注入 ACP prompt；URL 有效 1 小時、上限 50 MiB。agent 只能把這個 OpenAB 提供的
URL 原樣交給 `parse-document <url> <filename>`，自動取得 Markdown 後再回答。ZIP 同樣只能
用這個指令安全列出檔名與 metadata，不會解壓或讀取內容；video 仍不支援。

這個 URL trust boundary 是明確核准的取捨：presigned URL 會隨 ACP prompt 送到 company gateway。
agent 不得使用使用者文字提供的 URL、改寫 URL 或自行下載；`PARSE_DOCUMENT_ALLOWED_HOST` 只
限制 host，不能取代 R2 簽章或 Slack 身分驗證。完整支援矩陣與各格式限制見
[`docs/runbook.md` 的「文件附件支援矩陣」](docs/runbook.md#文件附件支援矩陣)。

## 輸入與輸出

| 方向 | 支援 | 不支援 |
|---|---|---|
| 輸入 | text、image、PDF、DOCX、XLSX、PPTX | audio、video；ZIP 僅列檔、不解析內容 |
| 輸出 | Slack 訊息、PNG、明確要求時的 self-contained HTML | Markdown 附件、patch、repo ZIP |

產物一律回原本的 Slack thread。

## Repo 內容

- `CONTEXT.md`：角色與領域詞彙，避免把 product-context bot、維護 agent和實作 agent混在一起。
- `CLAUDE.md`：給維護這個 deployment repo的 agent；不會 mount進 container。
- `docs/system-design.md`：完整系統設計 spec、信任邊界、流程與驗收情境。
- `.env.example`：snapshot root與 container使用者 uid/gid；複製成 root `.env`。
- `config/versions.env`：所有版本的正本 —— OpenAB image digest、OpenCode／OMO／CodeGraph、Claude ACP adapter、Claude Code CLI 與 Docling 版本。
- `Dockerfile`：在固定 OpenAB image上加入 Python 3、git、Docling、CodeGraph、OMO、固定版 Claude ACP adapter 與 Claude Code CLI，build 時預取 `/opt/docling-models` 並驗證 node 可讀，再把 container使用者的 uid對齊 host。
- `compose.yaml`：單一 OpenAB container，明確傳入 `DOCLING_ARTIFACTS_PATH=/opt/docling-models`。
- `config/openab.toml`：Slack 存取設定、session pool、Cloudflare R2 filestore，以及預設的
  `opencode acp` agent；R2 credentials 只用 `${R2_ACCESS_KEY_ID}`／`${R2_SECRET_ACCESS_KEY}`
  interpolation。
- `config/opencode/opencode.json`：OpenCode 的 provider、模型、instructions、skills 路徑與 permission。
- `config/opencode/oh-my-opencode-slim.json`：OMO preset（Terra synthesis／Luna retrieval）與 Claude Code ACP specialist。
- `agents/bin/company-image`：生圖／改圖的唯一入口，裝進 image 的受限 CLI，單一 runtime 共用。
- `agents/bin/slack-thread-artifact`：把 drafts 的 PNG／Markdown／HTML 用 bot token 回原 Slack thread 的受限 CLI，單一 runtime 共用。
- `tests/image-runtime.py`：用假 gateway 驗 `company-image` 的請求 shape、PNG 輸出與拒絕不合法輸入。
- `tests/slack-thread-artifact.py`：用假 Slack 驗 upload 三段流程的 header 與 body 編碼（兩個 Web API 呼叫送 form、bytes 那段不帶 token）、成功刪檔、失敗保留與拒絕不合法輸入。
- `tests/parse-document.py`：mock Docling 驗證 PDF artifacts gate、Office 路徑、實際格式 gate 與 URL／ZIP／輸出上限；不會下載真實模型，也不宣稱實際測到 multiprocessing worker 的 terminate timeout。
- `config/slack-home.json`：授權使用者看到的 Slack Home功能首頁。
- `config/repos.conf`：snapshot 清單的正本，新增 repo只改這裡。
- `agents/CLAUDE.md`：遠端 bot 的行為邊界；單一 runtime 共用同一份。
- `scripts/update-snapshots.sh`：host 端 clone/fetch/reset，並重建 CodeGraph 索引。
- `scripts/preflight.sh`：部署前檢查 secrets、版本 pin、目錄擁有權與 snapshot 狀態。
- `managed-claude-settings.json`：Claude specialist 使用的既有設定檔。
- `scripts/publish-slack-home.sh`：透過 Slack `users.list` 列出本 workspace 成員，並逐一發布 Home view；
  發布流程與 `users:read` scope 說明見 [`docs/runbook.md`](docs/runbook.md)。
- `scripts/install-sync-cron.sh`：建立目錄並寫入兩個每小時的 crontab entry（snapshot 同步、artifact cleanup）。
- `scripts/cleanup-artifacts.sh`：cron 呼叫的那一支，在 container 內執行 `slack-list cleanup` 收掉超過 24 小時的 artifact。
- `docs/runbook.md`：deployment host首次安裝、Slack 設定與日常操作。
- `docs/adr/`：不容易從設定本身看懂的決策理由。

AI agent修改前從 [`CLAUDE.md`](CLAUDE.md) 的閱讀順序開始。要理解系統目的而不是操作命令，讀 [`docs/system-design.md`](docs/system-design.md)。

## Local 驗證與 Smoke Test

```bash
./tests/static.sh
./tests/parse-document.py # parser unit；不測量 multiprocessing worker terminate timeout
git diff --check
```

`tests/static.sh` 只做靜態檢查。沒有 Docker 的機器上它會改用本機解析 `compose.yaml`，並在最後印出
「LIMITED VERIFICATION」列出沒驗到的項目 —— 那不是 container smoke test 通過。它跑的三支 mocked
測試（`image-runtime.py`、`slack-thread-artifact.py`、`artifact-path-safety.py`）都不呼叫 Slack 或
公司 gateway。

`scripts/deploy.sh` 也只是多加了 container 內的存在性與權限檢查。真的 Slack 往返、真的生圖、STT 與
cleanup 排程是否觸發，都在 [`docs/runbook.md`](docs/runbook.md#人工-release-gate) 的「人工 release
gate」，**必須由人各跑一次**；`deploy.sh` 退出 0 不代表那些過了，它結束時會把清單再印一次。

Local與deployment host共用同一套 runtime設定，不另建 local Compose。Local smoke test自己建一份乾淨 snapshot，
不掛開發用的 checkout；state與drafts都放在被 Git忽略的 `runtime/`。完整命令見
[`docs/runbook.md`](docs/runbook.md#2-local-首次啟動)。

目標機器填好 secrets 並完成首次 snapshot sync 後，再跑：

```bash
./scripts/preflight.sh
./scripts/deploy.sh
```

完整步驟見 [`docs/runbook.md`](docs/runbook.md)。

Docling 的 production baseline 與 deployment host 上的 build、node 權限及 no-download 檢查命令，見
[`docs/runbook.md`](docs/runbook.md#docling-production-runtime-baseline)。不要把 static test 或
artifact directory 檢查宣稱成 offline PDF conversion；目前不支援掃描 PDF OCR，未來需另加 OCR
engine；真實 PDF 附件仍須按 release gate 驗證。
