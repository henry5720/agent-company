# work-agent-deploy

在一台 Linux deployment host 上部署給 PM 共用的 Slack workspace agent。PM 在 Slack 拿定期更新的
repo snapshot 當參考做 prototype、問產品問題、查詢或建立指派給自己的待辦；產物（截圖、HTML
prototype、diff `.md`）回 Slack thread 當參考，不進 repo。agent 不能 push、不能建立 GitHub issue。

## 架構

```text
Slack
  -> OpenAB + codex-acp（bot container；最多 10 個 session，閒置 4 小時回收）
       -> host Caddy proxy（注入 gateway key、JSON access log 14 天）-> 公司 gateway（模型與生圖）
       -> /home/node/code/*（唯讀 repo snapshots，單一 mount）
       -> /home/node/work（thread workspace：每個 thread 自己的可寫 repo 副本）
       -> sandbox-run -> prototype sandbox ×2（沒有任何 token：install、dev server、截圖、附件轉換）
       -> /home/node/out（sandbox 的截圖與轉換結果，bot 唯讀）
       -> company-image -> /home/node/drafts（生圖 PNG）
       -> slack-thread-artifact -> 原 Slack thread
       -> Cloudflare R2 filestore（incoming attachments）

deployment host
  -> 防火牆：sandbox 只連外網，bot 只連 sandbox runner 與 proxy
  -> crontab：snapshot 同步、清理（drafts、thread workspace、out、CODEX_HOME sessions、pnpm store）、告警
  -> 專用 GitHub SSH key，只存在 host
```

agent 在 container 內 full access；container 沒有 GitHub token、SSH key、Docker socket，也沒有公司
gateway key。repo 的 code 只在 prototype sandbox 跑。Slack bot token 與 OpenAB 自己的 secret 在 bot
container 內，清單與取捨見 [`docs/system-design.md`](docs/system-design.md)「Runtime secret」。

## 輸入與輸出

| 方向 | 支援 | 不支援 |
|---|---|---|
| 輸入 | text、image、PDF、DOCX、XLSX、PPTX（經 prototype sandbox 轉換）；audio 只在 STT 啟用時 | video、ZIP、掃描 PDF OCR |
| 輸出 | Slack 訊息、prototype 截圖、HTML prototype、PNG 圖、包成 `.md` 的 diff | 預覽網址、patch 檔、push |

產物一律回原本的 Slack thread。

## Repo 內容

- `CONTEXT.md`：角色與領域詞彙。
- `CLAUDE.md`：給維護這個 deployment repo 的 agent；不會 mount 進 container。
- `agents/CLAUDE.md`：workspace agent 的 runtime contract，mount 成 Codex 的 `AGENTS.md`。
- `docs/system-design.md`：完整系統設計、信任邊界、流程與驗收情境。
- `docs/runbook.md`：deployment host 首次安裝、Slack 設定、切換與回滾、日常操作。
- `docs/adr/`：不容易從設定本身看懂的決策理由。
- `compose.yaml`：bot（`backlog-agent`）與兩格 prototype sandbox。
- `Dockerfile`、`sandbox/Dockerfile`：bot 與 sandbox 兩份 image。
- `config/versions.env`：所有版本與 image digest 的正本。
- `config/openab.toml`：Slack 存取、session pool、R2 filestore、`codex-acp` 與它的 env。
- `config/codex/`：Codex 的 provider 設定與 model catalog。
- `config/host-network.env`、`config/caddy/Caddyfile`、`config/systemd/`：host proxy 與防火牆。
- `config/repos.conf`：snapshot 清單的正本，新增 repo 只改這裡。
- `config/slack-home.json`：PM 在 Slack Home 看到的能力說明。
- `agents/bin/`：裝進 bot image 的受限 CLI（`company-image`、`slack-thread-artifact`、`thread-workspace`、`sandbox-run`）。
- `sandbox/`：sandbox 的 runner、截圖與附件轉換工具。
- `scripts/`：host 端的部署、安裝、同步、清理與告警腳本。
- `tests/`：`static.sh` 與它呼叫的 mocked 測試。

AI agent 修改前從 [`CLAUDE.md`](CLAUDE.md) 的閱讀順序開始。

## 驗證

```bash
./tests/static.sh
bash -n scripts/*.sh tests/*.sh
docker compose config --quiet
```

`tests/static.sh` 只做靜態檢查與 mocked 測試，不呼叫 Slack 或公司 gateway，也不啟動真的 sandbox 或
proxy。`scripts/deploy.sh` 只多驗 container 內的版本、mount 與權限。真的 Slack 往返、生圖、截圖流程與
排程觸發都在 [`docs/runbook.md`](docs/runbook.md#人工-release-gate) 的「人工 release gate」，**必須由人
各跑一次**。

部署步驟見 [`docs/runbook.md`](docs/runbook.md)。
