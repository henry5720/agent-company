# agent-company

這個 repo 部署 Slack 上多人共用的 company agent：OpenAB 轉接 Codex，拿公司產品 repo 的唯讀
snapshot 回答產品問題、讀寫 Slack 待辦，需要時 clone 一份來改、跑，再把 patch 或截圖交回 thread。
GitHub 寫入與 issue 留在 local。

## 先讀

1. `CONTEXT.md`：本 repo 的角色與詞彙。
2. `docs/system-design.md`：需求、信任邊界、流程、明確不做與驗收情境。
3. `docs/adr/`：不容易從設定本身看懂的決策理由。現行形狀見 `0012-follow-the-official-openab-codex-deployment.md`。
4. `docs/runbook.md`：只有部署或排障時需要讀。

## 兩份 agent 指引不同

- `/CLAUDE.md` 是給維護這個 deployment repo 的 agent，也就是你正在讀的檔案。
- `/agents/AGENTS.md` 是 company agent 的 bot 專屬規則。container 內 `agents/bin/agent-sync` 把它接在維護者個人規則（agent-config 的 `instructions/agents-md/AGENTS.md`）後面，寫成 `~/.codex/AGENTS.md`。只寫跟維護者機器不同的地方。

維護指令寫在這裡，Slack runtime 規則寫在 `agents/AGENTS.md`。

## 唯一正本

| 要改什麼 | 正本 |
|---|---|
| 個人 machine dotfiles 與 instructions | `chezmoi` 管理的 personal dotfiles（外部正本，不是本 deployment repo） |
| Bot 行為（runtime contract） | 個人規則在 agent-config；bot 專屬的在 `agents/AGENTS.md` |
| Bot 的 skills、MCP 怎麼裝 | `agents/bin/agent-sync` 與 `config/skillshare/config.yaml`（skillshare，跟維護者機器同一套） |
| 領域詞彙 | `CONTEXT.md` |
| 系統行為與能力邊界 | `docs/system-design.md` |
| 所有版本與 OpenAB image digest | `config/versions.env` |
| Container 資源與 mount | `compose.yaml` |
| Slack 存取、session 與 agent process | `config/openab.toml` |
| Codex provider、模型與 catalog | `config/codex/`（`config.toml` 是範本，`scripts/deploy.sh` 填入 gateway 位址） |
| 生圖／改圖能力邊界 | `agents/bin/company-image`（改介面等於改能力） |
| 檔案回 thread 的格式與路徑 | `agents/bin/slack-thread-artifact` |
| Snapshot remote 與基準 branch | `config/repos.conf` |
| Host 排程（snapshot 同步、artifact cleanup） | `scripts/lib.sh` 的 `render_crontab` |
| PM 在 Slack Home 看到的能力說明 | `config/slack-home.json`（改 `repos.conf` 或 bot 能力要一起看這份） |
| Skill、MCP 與個人規則 | `agent-config`（public repo；bot 與維護者機器共用） |
| Deployment host 操作步驟 | `docs/runbook.md` |

同一個值若必須出現在文件和設定，設定是機器正本；文件要連回設定，不要另造可獨立修改的清單。
bot 的 skills、MCP 與個人規則跟維護者機器用同一份 `agent-config`，deployment repo 不另抄、不另裁。

## 形狀照 OpenAB 官方

部署照 OpenAB 官方的 Codex 部署（ADR 0012）：一個 container、`/home/node` 是可寫的 named volume
`home`、mode `agent-full-access`、邊界是 container 本身。改設定前先查官方怎麼做
（`openabdev/openab` 的 `docs/codex.md`、`charts/openab/`），跟官方一致的做法優先，自己加規則要有理由並寫進 ADR。

跟官方不同、刻意保留的只有這幾項：

- Repo snapshot 以單一 mount 掛成 `/home/node/snapshots`，唯讀；`/home/node/snapshots/.index`（CodeGraph 索引）可寫，由 host 維護。agent 改 code 一律 clone 到 home。
- `~/.codex/config.toml` 由 `agent-sync` 管：只換 provider 段、保留 skillshare 寫的 `[mcp_servers.*]`。整份覆蓋會讓 skillshare 判定被別人改過而拒絕同步。不要把它改回唯讀 mount。
- `chrome` sidecar（OpenAB `docs/sidecar.md` 的模式）跟 agent 共用 localhost，Chrome 聽 `127.0.0.1:9222`，讓 agent-config `mcp.yaml` 的 chrome-devtools 設定原樣可用。sidecar 不放 token、不掛 volume。兩者都加入只持有網路的 `net`（k8s pause container）；不要改成 `network_mode: service:agent`，那樣 agent 一重啟 Chrome 就斷線。
- 生圖／改圖只走 `company-image`，參數維持 prompt、size、來源圖片。endpoint、header、model、輸出路徑留在 CLI 內；理由見 `docs/adr/0008-restricted-company-image-cli.md`。
- `company-image` 與 `slack-thread-artifact` 的路徑處理固定從 root 開 dirfd、逐段 `O_DIRECTORY|O_NOFOLLOW`；刪除用同一個 parent fd 並比對檔案 identity；寫入用 `O_CREAT|O_EXCL|O_NOFOLLOW` 加 temp→fsync→rename。`tests/artifact-path-safety.py` 會擋 `resolve()` 或字串比對後再開檔的寫法。

## 不可破壞的邊界

- Container 裡沒有 GitHub token 或 SSH key，也不掛 Docker socket。GitHub SSH key 只供 deployment host 的 snapshot 同步，不進 Compose env 或 volume。agent 的 code 改動以 `.patch` 交回 thread，由人 push。
- `WORK_HELPER_ISSUE_MODE` 必須是 `manual`。遠端 agent 不建立 GitHub issue，也不執行驗收回報。
- OpenAB image 固定 immutable digest，正本在 `config/versions.env`；Compose 的 checked-in build args 同步更新。升級時先確認新版本 Slack config 與 multi-arch manifest。Build 只用固定版本，Compose 可直接執行。
- Codex 與 codex-acp 以固定版本裝在 `/opt/codex-<ver>`，`config/openab.toml` 的 `CODEX_PATH` 指到同一版。
- 公司 gateway 只提供 Responses API：`config/codex/config.toml` 維持 `wire_api = "responses"`。`COMPANY_GATEWAY_BASE_URL` 是 gateway 提供 `/responses` 的前綴（不是 `/v1`），Codex 與 `company-image` 共用同一個值。理由見 `docs/adr/0010-company-provider-uses-the-responses-api.md`。
- Slack bot token、公司 gateway key 與 agent 執行的 repo code 在同一個 container，這是已決定的取捨（ADR 0012）。文件照實寫，不宣稱有 broker、proxy 或 token 隔離。
- 「產物回原本那個 Slack thread」只能寫成單一 container 內 OpenAB `sender_context`、agent 與受限 CLI 之間的信任約定，**不是安全保證**。理由見 `docs/adr/0009-thread-artifact-upload-and-optional-stt.md`。
- 上傳失敗的 artifact 由 host crontab 收。`scripts/cleanup-artifacts.sh` 維持呼叫 container 內的 `/home/node/snapshots/agent-config/skills/slack-list/scripts/slack-list cleanup`，刪除規則的正本在 agent-config。
- `tests/static.sh` 與 `scripts/deploy.sh` 只宣稱驗過離線能驗的事；Slack、公司 gateway、STT、cron 觸發屬於 `docs/runbook.md`「人工 release gate」。
- `allow_all_users = true` 讓同一個 Slack workspace 的使用者可驅動 agent；實際入口仍受 Slack app 收到的 DM、channel、thread 事件範圍限制。
- `allow_all_channels = true` 仍須維持；改這一項前先讀 `docs/system-design.md` 的「互動入口」。

## 修改規則

- 不提交 `env/openab.env`、credentials、private key 或真實 token。
- 改 Compose mount 時，同步檢查 nested mount 的深度、`home` volume 初次複製的 ownership（Dockerfile 先建好 mount point），與 host 目錄 ownership。
- 改 snapshot 路徑或 branch 時，同時更新 `config/repos.conf`、preflight 與 runbook。
- 改 Slack 能力時，同時檢查 bot events、OAuth scopes、OpenAB config 及人工驗收情境。
- 不直接在 deployment host 修 repo 內檔案；這個 repo 是部署設定的正本。
- 文件、issue 與 commit 一律用角色名稱「deployment host」稱呼部署機。實際是哪台不寫進 repo（repo 是 public，機器也會換）；要操作前先問維護者，或在候選機器上用 `docker ps --filter name=agent-company` 確認 container 真的在那裡。
- 沒有使用者明確要求，不建立 remote、不 commit、不 push、不部署。

## 驗證

Local 每次至少跑：

```bash
./tests/static.sh                       # 含 tests/*.py 三支 mocked 測試
bash -n scripts/*.sh tests/*.sh
docker compose config --quiet
```

沒有 Docker 的機器上，`tests/static.sh` 會在最後印出「LIMITED VERIFICATION」列出沒驗到的項目。
那不是 smoke test 通過，回報時要照實說。

有可用 deployment host secrets 與 snapshots 時，再跑 `./scripts/preflight.sh`。

`tests/static.sh` 會跑三支 mocked 測試（`tests/image-runtime.py`、`tests/slack-thread-artifact.py`、
`tests/artifact-path-safety.py`），沒有一支呼叫 Slack 或公司 gateway。

跑得過**不代表**這一版可以放給人用。人工能驗的清單在 `docs/runbook.md`「人工 release gate」，
與 `scripts/deploy.sh` 結尾印出的清單一致。回報時照實說哪些沒驗到，不要把 `static.sh` 或
`deploy.sh` 退出 0 講成 smoke test 通過。

## Agent skills

### Issue tracker

Issue 與 wayfinder map 放在 GitHub Issues（`henry5720/agent-company`），用 `gh` 操作。見 `docs/agents/issue-tracker.md`。

### Triage labels

用預設五個：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。見 `docs/agents/triage-labels.md`。

### Domain docs

只有一套術語：詞彙在 `CONTEXT.md`，決策在 `docs/adr/`。見 `docs/agents/domain.md`。
