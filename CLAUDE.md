# work-agent-deploy

這個 repo 部署 Slack 上給 PM 共用的 workspace agent：PM 拿 repo snapshot 當參考做 prototype、問產品問題、查或建立指派給自己的 Slack 待辦。repo 的 code 在不帶 token 的 prototype sandbox 跑；改碼進 repo、push 和 GitHub issue 留在 local。

## 先讀

1. `CONTEXT.md`：本 repo的角色與詞彙。
2. `docs/system-design.md`：需求、信任邊界、流程、明確不做與驗收情境。
3. `docs/adr/`：不容易從設定本身看懂的決策理由。
4. `docs/runbook.md`：只有部署或排障時需要讀。

## 兩份 CLAUDE.md 不同

- `/CLAUDE.md` 是給維護這個 deployment repo的 agent，也就是你正在讀的檔案。
- `/agents/CLAUDE.md` 會 mount 成 container 內 `/home/node/.codex/AGENTS.md`（Codex 的全域指示），是 workspace agent 的 runtime contract。

不要把維護指令寫進 `agents/CLAUDE.md`，也不要把 Slack runtime prompt塞進 root `CLAUDE.md`。

## 唯一正本

| 要改什麼 | 正本 |
|---|---|
| 個人 machine dotfiles 與 instructions | `chezmoi` 管理的 personal dotfiles（外部正本，不是本 deployment repo） |
| Bot runtime contract、provider/model 與 container mount | 本 deployment repo：`agents/CLAUDE.md`、`config/codex/`、`compose.yaml` |
| 領域詞彙 | `CONTEXT.md` |
| 系統行為與能力邊界 | `docs/system-design.md` |
| 所有版本與 agent runtime 選擇 | `config/versions.env` |
| Container資源與 mount | `compose.yaml` |
| Slack 存取、session與預設 agent process | `config/openab.toml` |
| Codex provider、模型、`base_url` 與 `wire_api` | `config/codex/config.toml` |
| Codex model catalog | `config/codex/catalog.json`（升級 codex 時重產） |
| Codex 的三個 env（`CODEX_HOME`、`CODEX_PATH`、`INITIAL_AGENT_MODE`） | `config/openab.toml` 的 `[agent].env` |
| bot、sandbox、host proxy 的網路位址 | `config/host-network.env` |
| host proxy 路由與 access log | `config/caddy/Caddyfile` |
| 防火牆規則 | `scripts/host-firewall.sh` |
| thread workspace 的路徑與清理規則 | `agents/bin/thread-workspace` |
| prototype sandbox 的 image、runner 與呼叫介面 | `sandbox/`、`agents/bin/sandbox-run` |
| 生圖／改圖能力邊界 | `agents/bin/company-image`（單一 runtime 共用，改介面等於改能力） |
| Snapshot remote與基準 branch | `config/repos.conf` |
| Host 排程（snapshot 同步、artifact／thread workspace／out 清理、pnpm store prune、告警） | `scripts/lib.sh` 的 `render_crontab` |
| PM 在 Slack Home 看到的能力說明 | `config/slack-home.json`（改 `repos.conf` 要一起看這份） |
| Bot行為 | `agents/CLAUDE.md` |
| Skill 與 MCP catalog 定義 | `agent-config`（public repo 的 canonical catalog；不代表這個 bot 已啟用或獲准使用） |
| Deployment host操作步驟 | `docs/runbook.md` |

同一個值若必須出現在文件和設定，設定是機器正本；文件要連回設定，不要另造可獨立修改的清單。
`agent-config` 的 skill／MCP catalog 是可見性與定義的正本，不是 runtime authorization。是否能由這個 bot
使用，由 `agents/CLAUDE.md` contract 與 mount 邊界決定（Codex 沒有 skill deny 設定）；
不要在 deployment repo 另抄一份 skill allowlist。

## 不可破壞的邊界

- Container 不得取得 GitHub token、SSH key 或 Docker socket，不開 privileged。可寫的宿主機 bind mount 只有 `compose.yaml` 列的 `runtime/` 子目錄與 snapshot root 的 `.index`，不再加別的。repo snapshot 唯讀；可寫的 repo 副本只有 thread workspace。
- Snapshot root 以單一 mount 掛成 `/home/node/code`，必須維持 read-only，也是 OpenAB 的全域 `working_dir`（agent 忘了切目錄時寫入直接失敗）。bot 可寫的只有 `/home/node/work`（thread workspace）、`/home/node/drafts`、`/home/node/code/.index`（CodeGraph 索引，host 維護、agent 只查詢）與 `CODEX_HOME` volume；`/home/node/out` 對 bot 唯讀。
- `CODEX_HOME`（`/home/node/.codex`）必須是 named volume `codex-home`，沒有它 `session/load` 會失敗、OpenAB 會默默開新 session。`config/codex/config.toml` 單檔唯讀疊在上面；不要改成把 `/home/node` 掛可寫 —— 那會讓底下所有唯讀 mount 失效。
- `WORK_HELPER_ISSUE_MODE` 必須是 `manual`。遠端 agent不建立 GitHub issue，也不執行驗收回報。
- GitHub SSH key只供 deployment host的 snapshot同步使用，不能進 Compose env或 volume。
- OpenAB image與 sandbox base image（`SANDBOX_NODE_IMAGE`）必須固定 immutable digest，正本在 `config/versions.env`。升級時先確認新版本 Slack config與 multi-arch manifest，再同步更新 spec和驗證。
- Build不得出現 `latest`、`beta`、`stable` 或任何浮動 tag，bot 與 sandbox 兩份 image 都一樣。Compose 可直接執行。
- `config/versions.env` 記錄所有版本與 immutable image digest；Compose 兩個 service 的 checked-in build args 必須同步更新。
- 生圖／改圖只走 `company-image`。key 在 host proxy，CLI 已不是安全邊界；不加 endpoint、header、model、輸出路徑這類參數是維護規則，讓 agent 只有一個固定、可測的入口。理由見 `docs/adr/0008-restricted-company-image-cli.md`。
- 公司 gateway 只提供 Responses API。`config/codex/config.toml` 的 `wire_api` 必須是 `"responses"`，`base_url` 指向 host proxy（`config/host-network.env` 的 `WORK_AGENT_GATEWAY_IP:WORK_AGENT_PROXY_PORT`）；`compose.yaml` 的 `COMPANY_GATEWAY_BASE_URL` 是同一個位址。理由見 `docs/adr/0013-codex-reaches-the-gateway-through-a-host-proxy.md`。
- codex 與 codex-acp 一起釘版裝在 `/opt/codex-<ver>`，`config/openab.toml` 的 `CODEX_PATH` 指到那裡；不設的話 codex-acp 會跑它自己底下那版。兩個一起升，升級時重產 `config/codex/catalog.json`（parse 失敗 codex 整個起不來）。
- 公司 gateway key 不在 container 內：只在 host 的 `/etc/work-agent/gateway.env`，由 host Caddy proxy 注入。不要放回 `env/openab.env`（`env_file` 會整份載進 bot container），preflight 會擋。Slack bot token 和 OpenAB 自己的 secret（`SLACK_APP_TOKEN`、R2、STT key）在 container 裡，清單見 `docs/system-design.md`「Runtime secret」。
- 「產物回原本那個 Slack thread」只能寫成單一 container 內 OpenAB `sender_context`、agent 與受限 CLI 之間的信任約定，**不是安全保證**。不要升級成 token isolation、cryptographic binding 或防 prompt injection 的說法，也不要改成 broker／relay。理由見 `docs/adr/0009-thread-artifact-upload-and-optional-stt.md`。
- 上傳失敗的 artifact 與 thread workspace、sandbox 產物、`CODEX_HOME` sessions 都由 host crontab 收，不是由文件收，全部由 `scripts/lib.sh` 的 `render_crontab` 產生；告警 cron（`scripts/alerts.sh`）也在同一份。drafts 的刪除規則正本在 agent-config：`scripts/cleanup-artifacts.sh` 必須維持呼叫 container 內的 `/home/node/code/agent-config/skills/slack-list/scripts/slack-list cleanup`，部署層不要自己寫第二份。刪除一律 `docker compose exec` 進擁有該目錄的 container 做。
- `tests/static.sh` 與 `scripts/deploy.sh` 都不得聲稱驗過 Slack、公司 gateway、STT 或 cron 觸發。那些是 `docs/runbook.md`「人工 release gate」的項目。
- `allow_all_users = true` 讓同一個 Slack workspace 的使用者可驅動 agent；實際入口仍受 Slack app 收到的 DM、channel、thread 事件範圍限制。
- `allow_all_channels = true` 仍須維持；改這一項前先讀 `docs/system-design.md` 的「互動入口」。
- prototype sandbox 不帶任何 token（沒有 `env_file`、沒有 `SLACK_*`／`COMPANY_*` env）、不掛 Docker socket、不 privileged，`runtime/work` 一定 `:ro`、只寫 `runtime/out`。host proxy 不對 sandbox 開放（防火牆只讓 bot 的固定 IP 連）。理由見 `docs/adr/0014-run-repo-code-in-the-prototype-sandbox.md`。
- 不可 `modprobe -r br_netfilter`：它會連 `bridge` 一起卸掉，實測讓正式 bot 斷線約 7.5 小時。
- snapshot 端禁止 `git gc --prune=now`：thread workspace 用 `git clone --shared` 借 snapshot 的物件，提早 prune 會讓 workspace 壞掉。

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

`tests/static.sh` 會跑 `tests/` 底下全部 mocked 測試（包含 `tests/thread-workspace.py`、
`tests/sandbox-run.py`、`tests/host-network.py`；`tests/artifact-path-safety.py` 留作一般回歸測試），
沒有一支呼叫 Slack 或公司 gateway，也沒有一支啟動真的 sandbox 或 proxy。

跑得過**不代表**這一版可以放給人用。這些只有人工能驗，清單在 `docs/runbook.md`「人工 release gate」：

- 經 OpenAB 的真實 Slack 往返（一般對話、tool call、`session/load` 續接）與授權邊界。
- `slack-thread-artifact` 從 drafts／work／out 打真的 Slack（含刻意失敗一次確認留檔）。
- `company-image generate` 經 host proxy 打真的公司 gateway（會計費）。
- thread workspace → `sandbox-run` 截圖的完整流程，與附件轉換。
- 清理與告警的 crontab entry 真的觸發過。
- STT（只有 `[stt].enabled = true` 時）。

回報時要照實說哪些沒驗到，不要把 `static.sh` 或 `deploy.sh` 退出 0 講成 smoke test 通過。

## Agent skills

### Issue tracker

Issue 與 wayfinder map 放在 GitHub Issues（`henry5720/work-agent-deploy`），用 `gh` 操作。見 `docs/agents/issue-tracker.md`。

### Triage labels

用預設五個：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。見 `docs/agents/triage-labels.md`。

### Domain docs

只有一套術語：詞彙在 `CONTEXT.md`，決策在 `docs/adr/`。見 `docs/agents/domain.md`。
