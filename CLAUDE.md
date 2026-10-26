# work-agent-deploy

這個 repo 部署 Slack 上的共享 product-context bot。它只回答產品問題，並能查詢或建立指派給自己的 Slack 待辦；產品實作和 GitHub issue 留在 local。

## 先讀

1. `CONTEXT.md`：本 repo的角色與詞彙。
2. `docs/system-design.md`：需求、信任邊界、流程、明確不做與驗收情境。
3. `docs/adr/`：不容易從設定本身看懂的決策理由。
4. `docs/runbook.md`：只有部署或排障時需要讀。

## 兩份 CLAUDE.md 不同

- `/CLAUDE.md` 是給維護這個 deployment repo的 agent，也就是你正在讀的檔案。
- `/agents/CLAUDE.md` 會 mount成 container內 `/home/node/CLAUDE.md`，是 product-context bot 的 runtime contract。

不要把維護指令寫進 `agents/CLAUDE.md`，也不要把 Slack runtime prompt塞進 root `CLAUDE.md`。

## 唯一正本

| 要改什麼 | 正本 |
|---|---|
| 個人 machine dotfiles 與 instructions | `chezmoi` 管理的 personal dotfiles（外部正本，不是本 deployment repo） |
| Bot runtime contract、provider/model/permission 與 container mount | 本 deployment repo：`agents/CLAUDE.md`、`config/opencode/`、`compose.yaml` |
| 領域詞彙 | `CONTEXT.md` |
| 系統行為與能力邊界 | `docs/system-design.md` |
| 所有版本與 agent runtime 選擇 | `config/versions.env` |
| Container資源與 mount | `compose.yaml` |
| Slack 存取、session與預設 agent process | `config/openab.toml` |
| Claude ACP specialist | `config/opencode/oh-my-opencode-slim.json` 的 `acpAgents`；共用 `managed-claude-settings.json` |
| OpenCode provider、模型、permission | `config/opencode/opencode.json` |
| OMO 模型分工 | `config/opencode/oh-my-opencode-slim.json` |
| Claude ACP specialist | `config/opencode/oh-my-opencode-slim.json` 的 `acpAgents`；login state 在 `claude-credentials` volume |
| 生圖／改圖能力邊界 | `agents/bin/company-image`（單一 runtime 共用，改介面等於改能力） |
| Snapshot remote與基準 branch | `config/repos.conf` |
| Host 排程（snapshot 同步、artifact cleanup） | `scripts/lib.sh` 的 `render_crontab` |
| PM 在 Slack Home 看到的能力說明 | `config/slack-home.json`（改 `repos.conf` 要一起看這份） |
| Bot行為 | `agents/CLAUDE.md` |
| Skill 與 MCP catalog 定義 | `agent-config`（public repo 的 canonical catalog；不代表這個 bot 已啟用或獲准使用） |
| Deployment host操作步驟 | `docs/runbook.md` |

同一個值若必須出現在文件和設定，設定是機器正本；文件要連回設定，不要另造可獨立修改的清單。
`agent-config` 的 skill／MCP catalog 是可見性與定義的正本，不是 runtime authorization。是否能由這個 bot
使用，仍由本 repo 的 OpenCode permission、OMO/runtime 設定、`agents/CLAUDE.md` contract 與 mount 邊界共同決定；
不要在 deployment repo 另抄一份 skill allowlist。

## 不可破壞的邊界

- Container不得取得 GitHub token、SSH key、Docker socket或可寫 repo checkout。
- Snapshot root以單一 mount掛成 `/home/node/code`，必須維持 read-only。Project資料中只有 `/home/node/drafts` 與 `/home/node/code/.index`（CodeGraph索引）可寫；repo源碼永遠不可寫。索引由 host維護，agent只能查詢。
- `OPENCODE_CONFIG_DIR`（`/home/node/.config/opencode`）本身必須可寫，`config/opencode/` 的每個設定檔必須是**單檔**唯讀 mount。整個目錄掛唯讀會讓 `opencode acp` 死在 `FileSystem.writeFile (.../.gitignore)`；把設定檔換成整目錄可寫會讓 agent 改得動 provider 與 permission。在 `config/opencode/` 加檔案要一起加 mount，`tests/static.sh` 會擋。
- OpenCode 的 state root `/home/node/.opencode` 也必須可寫，用專屬 named volume `opencode-state`。這是跟上一條**不同**的目錄，會用同一種方式殺掉 `opencode acp`。不要改成把 `/home/node` 掛可寫 —— 那會讓底下所有唯讀 mount 失效。也不要跟 `/home/node/.openab`（OpenAB state）搞混。
- `WORK_HELPER_ISSUE_MODE` 必須是 `manual`。遠端 agent不建立 GitHub issue，也不執行驗收回報。
- GitHub SSH key只供 deployment host的 snapshot同步使用，不能進 Compose env或 volume。
- OpenAB image必須固定 immutable digest，正本在 `config/versions.env`。升級時先確認新版本 Slack config與 multi-arch manifest，再同步更新 spec和驗證。
- Build不得出現 `latest`、`beta`、`stable` 或任何浮動 tag。Compose 可直接執行。
- `config/versions.env` 記錄所有版本與 OpenAB immutable image digest；Compose 的 checked-in build args 必須同步更新。
- 生圖／改圖只走 `company-image`。不要加 endpoint、header、model、輸出路徑這類參數，也不要在文件裡教 agent 直接 `curl` gateway。理由見 `docs/adr/0008-restricted-company-image-cli.md`。
- 公司 gateway 只提供 Responses API。`config/opencode/opencode.json` 的 `provider.company.npm` 必須是 `@ai-sdk/openai`；換成 `@ai-sdk/openai-compatible` 會讓 OpenCode 打到 `{baseURL}/chat/completions`，gateway 回 HTTP 405，bot 整台問不動。`COMPANY_GATEWAY_BASE_URL` 是那個 gateway 提供 `/responses` 的前綴（不是 `/v1`），provider 與 `company-image` 共用同一個值、接同一個後綴。`tests/provider-route.py` 會擋，理由見 `docs/adr/0010-company-provider-uses-the-responses-api.md`。
- Claude ACP adapter 與 Claude Code CLI 使用固定版本，由 Docker build 安裝；OMO 不得改回 runtime `npx` download。Claude login state 使用既有 `claude-credentials` named volume。
- Slack bot token 與公司 gateway key 共用同一個 container，這是已決定的取捨。不要在文件裡宣稱有 broker 或 token 隔離。
- 「產物回原本那個 Slack thread」只能寫成單一 container 內 OpenAB `sender_context`、agent 與受限 CLI 之間的信任約定，**不是安全保證**。不要升級成 token isolation、cryptographic binding 或防 prompt injection 的說法，也不要改成 broker／relay。理由見 `docs/adr/0009-thread-artifact-upload-and-optional-stt.md`。
- `agents/bin/` 兩支 CLI 的路徑處理不得回頭用 `resolve()` 或字串比對後再開檔。固定從 root 開 dirfd、逐段 `O_DIRECTORY|O_NOFOLLOW`；刪除用同一個 parent fd 並比對檔案 identity；寫入用 `O_CREAT|O_EXCL|O_NOFOLLOW` 加 temp→fsync→rename。`tests/artifact-path-safety.py` 會擋。
- 上傳失敗的 artifact 由 host crontab 收，不是由文件收。`scripts/cleanup-artifacts.sh` 必須維持呼叫 container 內的 `/home/node/code/agent-config/skills/slack-list/scripts/slack-list cleanup`，刪除規則的正本在 agent-config，部署層不要自己寫第二份。
- `tests/static.sh` 與 `scripts/deploy.sh` 都不得聲稱驗過 Slack、公司 gateway、STT 或 cron 觸發。那些是 `docs/runbook.md`「人工 release gate」的項目。
- `allow_all_users = true` 讓同一個 Slack workspace 的使用者可驅動 agent；實際入口仍受 Slack app 收到的 DM、channel、thread 事件範圍限制。
- `allow_all_channels = true` 仍須維持；改這一項前先讀 `docs/system-design.md` 的「互動入口」。

## 修改規則

- 不提交 `env/openab.env`、credentials、private key或真實 token。
- 改 Compose mount時，同步檢查 root filesystem、nested mount與 host目錄 ownership。
- 改 snapshot路徑或 branch時，同時更新 `config/repos.conf`、preflight與 runbook。
- 改 Slack能力時，同時檢查 bot events、OAuth scopes、OpenAB config及人工驗收情境。
- 不直接在 deployment host修 repo內檔案；這個 repo是部署設定的正本。
- 文件、issue 與 commit 一律用角色名稱「deployment host」稱呼部署機。實際是哪台不寫進 repo（repo 是 public，機器也會換）；要操作前先問維護者，或在候選機器上用 `docker ps --filter name=work-agent` 確認 container 真的在那裡。
- 沒有使用者明確要求，不建立 remote、不 commit、不 push、不部署。

## 驗證

Local每次至少跑：

```bash
./tests/static.sh                       # 含 tests/image-runtime.py
bash -n scripts/*.sh tests/*.sh
docker compose config --quiet
```

沒有 Docker 的機器上，`tests/static.sh` 會改用本機解析 `compose.yaml`，並在最後印出
「LIMITED VERIFICATION」列出沒驗到的項目。那不是 smoke test 通過，回報時要照實說。

有可用 deployment host secrets與 snapshots時，再跑：

```bash
./scripts/preflight.sh
```

涉及 runtime權限的改動，必須再跑 `docs/runbook.md` 的「驗證安全邊界」，不能只看 Compose render成功。

`tests/static.sh` 會跑四支 mocked 測試（`tests/image-runtime.py`、`tests/slack-thread-artifact.py`、
`tests/artifact-path-safety.py`、`tests/provider-route.py`），沒有一支呼叫 Slack 或公司 gateway。
`provider-route.py` 驗的是 provider 設定會被路到哪個 endpoint，靠的是抄下來的 adapter 對應表，
不是真的請求 —— 它證明不了 gateway 收不收。

跑得過**不代表**這一版可以放給人用。這些只有人工能驗，清單在 `docs/runbook.md`「人工 release gate」：

- 真的 Slack 往返與授權邊界。
- `slack-thread-artifact` 打真的 Slack（含刻意失敗一次確認留檔）。
- `company-image generate` 打真的公司 gateway（會計費）。
- artifact cleanup 的 crontab entry 真的觸發過（`artifact-cleanup.log` 沒有 `FAILED`）。
- STT（只有 `[stt].enabled = true` 時）。

回報時要照實說哪些沒驗到，不要把 `static.sh` 或 `deploy.sh` 退出 0 講成 smoke test 通過。

## Agent skills

### Issue tracker

Issue 與 wayfinder map 放在 GitHub Issues（`henry5720/work-agent-deploy`），用 `gh` 操作。見 `docs/agents/issue-tracker.md`。

### Triage labels

用預設五個：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。見 `docs/agents/triage-labels.md`。

### Domain docs

只有一套術語：詞彙在 `CONTEXT.md`，決策在 `docs/adr/`。見 `docs/agents/domain.md`。
