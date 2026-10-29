# Shared Workspace Agent System Design

## 目標

在 Slack 提供給 PM 共用的 workspace agent：PM 拿 repo snapshot 當參考做 prototype、問產品問題、
查待辦、建立指派給自己的待辦，不必經由另一個人轉述。agent 在 container 內 full access（shell、
網路）；container 擋宿主機，不擋 container 內的東西。

產物（截圖、HTML prototype、diff `.md`）回 Slack thread，作為「PM 想要長這樣」的參考，不進 repo。
共用 bot 沒有 GitHub 寫入：本地 git 可用、不能 push、不建立 GitHub issue，也不通知 PM 驗收。
決策理由見 [`adr/0012-shared-workspace-agent-for-pms.md`](adr/0012-shared-workspace-agent-for-pms.md)。

## 角色

| 角色 | 能做什麼 | 不能做什麼 |
|---|---|---|
| PM（Slack workspace 使用者） | 從 Slack 發問、要 prototype、查詢或建立自己的待辦 | 透過遠端 bot push、取得 GitHub credential |
| workspace agent | 讀 repo snapshot、在 thread workspace 改 code、經 prototype sandbox 截圖、讀寫待辦、回答產品問題 | push、建立 issue、查 private issues、回報驗收 |
| prototype sandbox | 跑 repo 的 install、dev server／Storybook、截圖、附件轉換 | 拿到任何 token、寫 thread workspace、連 host proxy 或內網 |
| snapshot 同步者 | 在 deployment host 從 GitHub 更新 snapshots | 進入 container 或代表 agent 發布內容 |
| 實作 agent（維護者 local） | 建 issue、修改 code、測試、push、回報驗收 | 假裝 prototype 等於已實作 |

Slack 待辦的回報對象、指派對象，沿用 agent-config `skills/slack-list/SKILL.md` 的定義。

## 系統邊界

```text
Slack ──Socket Mode──▶ bot container（OpenAB + codex-acp；帶 SLACK_BOT_TOKEN）
                         │  working_dir = /home/node/code（唯讀 snapshot）
                         │  寫：/home/node/work（thread workspace）、/home/node/drafts、CODEX_HOME volume、.index
                         │  讀：/home/node/out（sandbox 寫的截圖／轉換結果）
                         │
                         ├─ POST /responses（模型與生圖）──▶ host Caddy proxy（注入 gateway key、JSON log 14 天）──▶ 公司 gateway
                         ├─ Slack Web API、Slack Lists API
                         ├─ Cloudflare R2 filestore（incoming attachments，由 OpenAB 讀）
                         │
                         └─ sandbox-run（內部網路，只開 runner port）
                               ▼
                       prototype sandbox ×2（沒有任何 token；node 身分；唯讀掛 runtime/work；只寫 runtime/out）

GitHub ──▶ deployment host 的 snapshot 同步者 ──▶ 唯讀 repo snapshots
local 實作 agent ──▶ GitHub issues / 可寫 worktree / push
```

host 目錄：

```text
runtime/work/<channel_id>-<thread root ts>/repos/<repo>/   bot 可寫（/home/node/work）；sandbox 唯讀掛整個 runtime/work
runtime/out/<channel_id>-<thread root ts>/                 sandbox 可寫；bot 唯讀（/home/node/out）
runtime/drafts/                                            bot 可寫（/home/node/drafts）：company-image 輸出
runtime/openab/                                            OpenAB state（/home/node/.openab）
$SNAPSHOT_ROOT/<repo>                                      bot 唯讀（/home/node/code，也是全域 working_dir）
```

thread 之間只分開 session（process），檔案與權限共用：同一個 container 裡，一個 thread 讀得到
另一個 thread 的 workspace。這是接受的取捨。

GitHub credential 只到 deployment host 與 local 實作環境，不跨進 bot container。詳見
[`adr/0001-github-access-stops-at-the-host-boundary.md`](adr/0001-github-access-stops-at-the-host-boundary.md)。

Deployment host 是一台 Linux host，Compose 以維護者自己的帳號執行；防火牆與 host proxy 要 sudo 安裝。
它同時是維護者的開發機，見下面「Runtime secret」。

## 網路與 host proxy

位址正本 `config/host-network.env`；規則與理由在 `scripts/host-firewall.sh`、`config/caddy/Caddyfile`。

- bot 與兩格 sandbox 接在 external network `work-agent-net`（bridge `br-work-agent`、`10.231.0.0/24`、
  `enable_icc=false`）。bot 固定 `10.231.0.10`，sandbox 從 `10.231.0.128/25` 動態配發。
- host Caddy proxy 綁 bridge IP `10.231.0.1:18080`，只放行 `POST /responses`、補上 `Authorization`，
  其他路徑 403。bot 的 `COMPANY_GATEWAY_BASE_URL` 與 Codex `base_url` 都指向它。
- 防火牆是兩條自訂鏈 `WORK-AGENT-FORWARD`／`WORK-AGENT-INPUT`，各從 `DOCKER-USER`／`INPUT` 第一位
  jump，全部 `-i br-work-agent` 限定：sandbox 只連得到外網（擋 host proxy、私有 IP 段、bot、EC2
  IMDS），bot 只連得到 sandbox 的 runner port 與 proxy。由 systemd oneshot unit
  `work-agent-firewall.service` 開機重下。不用 ufw，因為 Docker 會繞過 ufw。
- IMDS 主要靠防火牆擋 `169.254.169.254`，hop limit 1 當第二層。
- sandbox 的 DNS 用 `1.1.1.1`、`8.8.8.8`（VPC resolver 被擋）。
- `br_netfilter` 載入後不可卸載：卸載會連帶拿掉 `bridge`，實測讓正式 bot 斷線約 7.5 小時。

理由見 [`adr/0013-codex-reaches-the-gateway-through-a-host-proxy.md`](adr/0013-codex-reaches-the-gateway-through-a-host-proxy.md)
與 [`adr/0014-run-repo-code-in-the-prototype-sandbox.md`](adr/0014-run-repo-code-in-the-prototype-sandbox.md)。

## Runtime secret

| secret | 在哪 | 誰讀得到 | 外流後能做什麼 | 縮權／觀測 |
|---|---|---|---|---|
| `SLACK_BOT_TOKEN` | bot container（agent env，`config/openab.toml` 的 `inherit_env`） | container 內所有 thread 的 agent | 以 bot 身分在它進得去的 conversation 讀寫訊息、檔案、Lists | 縮到 10 個 scope（下方） |
| `SLACK_APP_TOKEN` | OpenAB process env | agent 經 `/proc/<openab pid>/environ`（同 uid） | 開 Socket Mode 連線收 bot 的事件 | 只留 `connections:write` |
| R2 key | OpenAB process env | 同上 | 讀寫 R2 bucket | 縮到只能用 `work-agent-attachments` bucket |
| STT key | OpenAB process env（啟用時） | 同上 | 用 STT endpoint 的額度 | 預設關閉 |
| gateway key | **只在 host Caddy**（`/etc/work-agent/gateway.env`），不在 container | 無；但 container 經 proxy 可以花額度 | — | 由下面「資源上限與用量告警」看 |

- 「經 `/proc` 讀得到」是依 Linux `/proc` 權限推論，**未實測**（列在 runbook 人工 release gate）。
  不選換 uid：官方 image 與 ACP 子程序模型不是這樣設計。
- **維護者開發機的憑證**：deployment host 同時是維護者的開發機，host 上有可 push 的 `gh` token
  （`repo` scope）、個人 SSH key 與 Claude 登入憑證。沿用這台、不另開專用機器是維護者 2026-10-27
  決定的取捨；agent 與這些憑證之間只隔 container 邊界（不 privileged、不掛 Docker socket、只有
  `runtime/` 與 `.index` 可寫），container 被逃出就等於拿到維護者的 GitHub 權限。
- **bot token scope**：`app_mentions:read chat:write files:read files:write groups:history im:history
  lists:read lists:write reactions:write users:read`；event 維持 `app_mention`、`message.groups`、
  `message.im`。`chat:write.public`、`channels:manage`、`groups:write.invites` 與沒有呼叫者的
  `groups:read` 已移除。
- **降 scope 一定要先 `auth.revoke` 或 uninstall 再裝**：Slack 不能用 Reinstall 降 scope。token 會換新、
  bot 會被移出所有 channel，要重新邀回。步驟見 [`runbook.md`](runbook.md)「切換與回滾」。
- 未驗：List 留言串所在 channel 需要 `groups:history` 還是 `channels:history`、兩個 channel 是否私有。
  讀不到時要補的是 `*:history`，不是 `groups:read`。

## 互動入口

- 同一個 Slack workspace 內使用者與 Slack app 的 DM。
- Slack app 實際能收到事件的 channel，以及其中的 thread。

`allow_all_users = true` 讓同一個 Slack workspace 內的使用者在 Slack 事件送達時都能驅動 agent，不以固定 user ID allowlist 篩選。實際入口仍受 Slack app 能收到的 DM、channel 與 thread 事件範圍限制；新增 app 能收到事件的 channel 等於擴大互動入口，必須當成權限變更 review。縮權後 bot 沒有 `chat:write.public`，「靠邀請維持 channel 邊界」才成立。

所有能透過 Slack 事件驅動 agent 的 workspace 使用者遠端能力相同，也都拿不到 GitHub credential。

## Repo Snapshots

清單的正本是 [`config/repos.conf`](../config/repos.conf)，一行一個 repo。整個 snapshot root 以單一 read-only mount 掛成 container 內的 `/home/node/code`，所以新增 repo 只改那一個檔案，不動 Compose。`/home/node/code` 也是 OpenAB 的全域 `working_dir`：agent 忘了切到 thread workspace 時寫入直接失敗。

Host 每小時 fetch 所有 remote refs，再把每個 working snapshot reset 到基準 branch。所有 OpenAB sessions 共用同一份內容。snapshot 端禁止 `git gc --prune=now`（thread workspace 借用它的物件）。

Snapshot 代表最近一次同步成功的狀態，不保證和 GitHub 當下完全同步。回答若依賴剛 push 的 commit，必須先確認 snapshot 同步完成。

workspace agent 查符號與影響範圍時用 CodeGraph 索引，找字串才用 grep。索引內容對應 snapshot 當下的基準 branch，不含 thread workspace 的修改。

Working tree 停在基準 branch，但 `.git` 內保有完整的 `origin/*` refs。agent 可以用 `git show origin/<branch>:<path>` 這類唯讀方式讀還沒合併的 branch；在 snapshot 上不能 checkout，在 thread workspace 裡可以。回覆引用時必須標明 branch 與 commit。

## Thread workspace

- **路徑**：`/home/node/work/<channel_id>-<thread root ts>/repos/<repo>/`。thread root ts 取 sender context 的 `thread_ts`，頂層訊息取 `message_id`。帶 channel 是為了跟 `slack-thread-artifact` 回貼目標同一組識別（OpenAB pool key 不含 channel）。
- **誰建**：受限 helper `thread-workspace <channel_id> <thread_ts> <repo>`：驗參數格式、組路徑、已存在就回傳原路徑、不存在就從 `/home/node/code/<repo>` 做 `git clone --shared`。路徑不給 LLM 拼。
- **何時建**：lazy，要改 code 才建；純問答直接讀 snapshot。一次一個 repo、snapshot 當下 HEAD、完整 checkout。commit 只為了做 diff。
- **跟 snapshot 同步**：不自動跟；PM 明確要最新才 `git fetch origin` + rebase，衝突不硬解，請 PM 選「從最新重開」或「留在舊 base」。
- **清理**：host cron 每小時 `docker compose exec` 進 bot 跑 `thread-workspace cleanup`（不從 host 直接刪）：最後修改超過 7 天刪；總量超過 20 GB 從最久沒動的刪起，並在該 thread 貼「工作副本已清除，再改會從最新 snapshot 重來」。不設 per-thread quota。
- **git 物件**：7 天短於 gc 預設 14 天 prune，`--shared` 不會壞。
- OpenAB 原生不提供 per-thread 工作目錄（沒有 `per_thread_workdir`、沒有 per-session hook，thread id 只在 prompt 的 `<sender_context>`），所以由 helper 做。

## Prototype sandbox

- **形狀**：Compose 常駐 service ×2（`prototype-sandbox-1`、`prototype-sandbox-2`），各跑一個小 HTTP runner（`sandbox/runner`）；bot 用 `sandbox-run <thread workspace 內的 repo 路徑> -- <指令>` 經內部網路呼叫，指令不設限。不掛 Docker socket、不用 `docker exec`、不 privileged。
- **檔案**：唯讀掛整個 `runtime/work`；每次執行先把原始碼複製（不帶 `.git`）到自己的 scratch 再 install。只寫 `runtime/out/<key>/`。sandbox 若能寫 bot 看得到的 code 就能種 `.git/hooks`，所以一定唯讀。
- **執行**：job 只拿到 runner 白名單裡的 env；指令結束或逾時（上限 15 分鐘）後，runner 把殘留 process（包括背景 dev server）全部收掉。同一個 thread 先試同一格，保住那格的 `node_modules`；忙碌時換格。
- **支援範圍**：只做 dev server／Storybook，不做 production build；repo 自己的 e2e 不支援。**不開預覽網址**：PM 拿到的是截圖與 HTML prototype。
- **依賴快取**：2 格共用一個 pnpm store named volume，每週 `pnpm store prune`；每格一個 named volume 放 scratch 與 `node_modules`，換 thread 清空。
- **產物清理**：`runtime/out` 一律留 24 小時，由 host cron `exec` 進 sandbox 跑 `sandbox-runner cleanup-out`（out 對 bot 唯讀，bot 刪不掉）。
- **附件**：PDF／DOCX／XLSX／PPTX 由 `sandbox-run --thread <channel_id> <thread_ts> -- sandbox-attachment <url> <filename>` 下載 OpenAB 給的 presigned URL、以 `npm exec` 現裝釘版 officeparser、轉成 Markdown 寫進 `runtime/out/<key>/`。不預裝進 image（現裝約 5 秒）；不做 OCR。

## Prototype 輸出規則

規則正本在 [`agents/CLAUDE.md`](../agents/CLAUDE.md)「Prototype 規則」。要點：

- 預設 **app prototype**：在 thread workspace 改真的 app，丟 sandbox 起 dev server，貼每個 variant 的截圖（需要時加關鍵狀態與手機寬度）。
- 現有 app 跑不出來（全新流程、後端邏輯、mobile 原生）、PM 明確要單獨檔案、或只演示邏輯或流程時，改做 **HTML prototype**：單一檔案、不連外部資源、直接上傳 thread。
- agent 照規則選，第一則回覆講出選了哪個、為什麼；PM 可要求改。一個需求最多 3 個 variant，用 `?variant=` 切換。
- 資料用 agent 自己寫的假 fixture，不接真實後端。
- PM 說要給工程師看或 prototype 定案時，才把 `git diff` 包進 `.md` 上傳；不新增 `.patch` 類型。

## 待辦流程

### 從 item 留言串開始

1. OpenAB 把 sender、`channel_id` 與 `thread_ts` 放進 prompt 的 `<sender_context>`；它不是 ACP side-channel。
2. workspace agent 用 `slack-list context` 反查唯一的待辦列。
3. agent 讀待辦列、完整 item 留言串與相關 repo snapshots。
4. agent 在同一個 item 留言串回答，不改待辦狀態。

### 從 DM 或一般 channel 開始

workspace agent 先找對應的既有待辦列；找不到而使用者明確要求建立時，
用`openab.sender.v1`的sender、channel與thread context建立一筆指派給sender的待辦列。標題由agent濃縮時先確認，
使用者明確提供標題時可直接建立；同名active列存在時加入既有assignees，不建立第二列。

`slack-list add`只由這個OpenAB instance執行，local implementation agent不執行，讓同一runtime的process lock
序列化查重與寫入。建立後的來源與回報設定留在原生item留言串；來源註記失敗時保留已建立的列並明確回報，
不直接重試。

### 「我的待辦」

這是多人共用的 agent，不能用固定的個人 ID判斷「我」。必須把當次
`openab.sender.v1.sender_id` 傳給 `slack-list assigned`，精確比對待辦列的 assignee user ID。

## 輸入與輸出邊界

輸入：text、image、audio（STT 啟用時）；PDF、DOCX、XLSX、PPTX 經 OpenAB 的 R2 文件流程交給
prototype sandbox 轉換；ZIP 與 video 不支援。

OpenAB 的 incoming attachment filestore 使用 Cloudflare R2：bucket `work-agent-attachments`、endpoint
`https://99de68928da234ebcf0c9370443ad7ee.r2.cloudflarestorage.com`、region `auto`、prefix
`incoming/`、presigned URL TTL 3600 秒與單檔上限 **50 MiB**（正本 `config/openab.toml` 的
`[filestore]`）。R2 access key 與 secret key 只透過 `${R2_ACCESS_KEY_ID}`／`${R2_SECRET_ACCESS_KEY}`
interpolation 從未追蹤的 `env/openab.env` 取得。R2 bucket 必須在外部設定 `incoming/` prefix 的 1 天
lifecycle expiration；preflight 以 interactive checklist 要求人工確認。

附件流程：OpenAB 收到 PDF／DOCX／XLSX／PPTX → 把 R2 presigned URL 與原始 filename 注入 prompt →
agent 原樣交給 `sandbox-attachment`（經 `sandbox-run --thread`）→ sandbox 下載、驗檔頭、轉 Markdown
寫進 out → agent 讀 `/home/node/out/<key>/<檔名>.md` 回答。agent 只能使用 OpenAB 注入的 URL，不得
使用使用者文字中的 URL、改寫 URL 或轉給其他服務。真實 OpenAB／R2 附件經這條路**未實測**，列在人工
release gate。

輸出：Slack 訊息本文、prototype 截圖（PNG）、HTML prototype、`company-image` 的 PNG、包成 `.md` 的
diff。所有產物回到發問的那個 Slack thread。

### 生圖

圖片**產出**走 `company-image`：一支裝在 image 內的受限 CLI。它只有 `generate` 與 `edit` 兩個 mode，
只收 prompt、允許清單內的 size 與（`edit` 用的）來源圖片路徑；沒有 endpoint、header、model 或輸出路徑
參數。請求送到 `COMPANY_GATEWAY_BASE_URL`（host proxy），固定用 Responses API `image_generation` tool，
固定帶 `User-Agent: work-agent-company-image`（用量告警靠它在 access log 分出生圖）；不讀 key、不送
`Authorization`。輸出固定是 PNG，固定寫到 `/home/node/drafts/<UTC 日期>/`。回應的 JSON body 與
`text/event-stream` SSE 兩種形狀都吃。key 在 proxy，所以這支 CLI 不是安全邊界，收窄的介面是為了讓
agent 有一個固定、可測的入口。理由見
[`adr/0008-restricted-company-image-cli.md`](adr/0008-restricted-company-image-cli.md)。

### 回傳檔案

產物用 `slack-thread-artifact` 回原 thread。它固定執行 `files.getUploadURLExternal → upload →
files.completeUploadExternal`，只讀 `/home/node/drafts`、`/home/node/work`、`/home/node/out` 底下的
regular PNG、Markdown、HTML。上傳成功只刪 drafts 的檔；work 由 thread workspace 清理、out 由 sandbox
清理。drafts 上傳失敗的檔保留，由 deployment host 的 `# work-agent-artifact-cleanup` crontab entry
（`scripts/cleanup-artifacts.sh` → container 內 `slack-list cleanup`，每小時 :17）收掉；保留期限
24 小時，檢查每小時一次，所以實際壽命是 24 到 25 小時。

兩個 Slack Web API 呼叫的 body 是 `application/x-www-form-urlencoded`，不是 JSON —— JSON body 會被
`files.getUploadURLExternal` 回 `invalid_arguments`。中間上傳 bytes 的那一段不是 Web API：只用 Slack
回的 upload URL、artifact mime 與原始 bytes，不帶 token。細節見
[`adr/0009`](adr/0009-thread-artifact-upload-and-optional-stt.md)。

### 「回原 thread」靠的是什麼

**這是信任約定，不是安全保證。** 說清楚它由什麼組成：

- OpenAB 把當次的 sender、`channel_id`、`thread_ts` 寫進 prompt 裡的 `<sender_context>`。那是
  **prompt content**，不是 ACP side-channel，也不是不可竄改的授權資料。
- agent 讀那段文字，再把 `--channel`、`--thread-ts` 逐字交給 CLI。這一步由 `agents/CLAUDE.md`
  的文字約束，不由任何機制強制。
- CLI 只檢查格式（`C/D/G` 開頭的 channel ID、Slack timestamp），不能判斷這組值是不是本次
  sender 真正的 thread。
- 三者在**同一個 container** 內，共用同一個 `SLACK_BOT_TOKEN`。

所以能被信任的原因是「這三段都在同一個信任域內」，不是任何一段驗證了另一段。**不宣稱**
token isolation、cryptographic binding，也**不宣稱**擋得住 prompt injection：能改變 agent 送出什麼
值的輸入，就能改變檔案送到哪個 thread（限於這個 bot token 本來就進得去的 conversation）。

刻意**不**改成 broker 或 relay：那要另一個 process 持有 token 並自己判斷 thread 歸屬，而
判斷依據仍然只有 OpenAB 給的同一份 context，換不到新的保證，只多一層要維護的東西。

### Audio

Audio 只有獨立的 OpenAI-compatible STT endpoint 已被確認、設定 `STT_BASE_URL`／`STT_API_KEY` 並把
`config/openab.toml` 的 `[stt].enabled` 改成 `true` 時才可轉錄。OpenAB 固定使用該 base URL 的
`/audio/transcriptions`；不假設 company gateway 支援。預設未啟用時 bot 明確拒絕 audio。

## 對話回覆

一般 Slack 對話先回答產品結論、使用者目前會遇到什麼，以及期望改成什麼。除非使用者明確追問，否則不在對話回覆附檔名、行號、函式名、state、payload 或 API。

Runtime 或部署故障造成工具不能執行時，workspace agent 只告知哪項查詢暫時不可用，並指出需要部署維護者修復，不要求 Slack 使用者執行 container 排障或安裝套件。

## Agent Runtime 與版本

- OpenAB 留著，`config/openab.toml` 的 `[agent].command = "codex-acp"`，`working_dir = "/home/node/code"`。
  三個 env 都從 `[agent].env` 給（會蓋過 `inherit_env`，且在 `env_clear()` 之後加入）：
  - `INITIAL_AGENT_MODE=agent-full-access`：container 內 codex 的 bwrap 建不了 namespace，預設 `agent`
    mode 每條 shell 都失敗，`workspace-write` 每條都要人按允許。可寫範圍交給 Docker mount 決定。OpenAB
    不呼叫 `session/set_mode`，初始 mode 只能用 env。
  - `CODEX_PATH` 指到 `/opt/codex-<ver>` 的釘版 codex；不設的話 codex-acp 會跑自己底下那版。
  - `CODEX_HOME=/home/node/.codex`，named volume `codex-home`；沒有它 `session/load` 失敗，OpenAB 會
    直接開新 session、不告訴使用者。
- Codex 設定在 `config/codex/config.toml`（provider `company`、`wire_api = "responses"`、`base_url`
  指 host proxy），單檔唯讀疊在 `CODEX_HOME` 上；model catalog `config/codex/catalog.json` 只放
  `gpt-6.1-sol` 一筆（消掉每輪回覆開頭的 model metadata warning），唯讀掛 `/etc/codex/catalog.json`。
  兩檔都不 build 進 image。
- codex memories 維持預設關閉，避免內容跨 thread 帶過去。
- runtime contract 是 `agents/CLAUDE.md`，唯讀掛成 `/home/node/.codex/AGENTS.md`。
- 切換前的 thread 不延續：舊 runtime 的 session 不轉換，再 @bot 會開新 Codex session。

版本規則：只有一個 `config/versions.env`；base image 帶 digest；點名的 npm／pip／Playwright 用 exact
版本；apt 不逐一釘、跟著 base digest 走；**codex 與 codex-acp 一起升，升級時重產 catalog**（catalog
parse 失敗 codex 整個起不來）。`compose.yaml` 使用 checked-in build args，因此 `docker compose` 可直接
render；`tests/static.sh` 比對兩邊。

```text
bot image（Dockerfile）
  FROM OPENAB_IMAGE（OpenAB codex image，帶 digest）
  + /opt/codex-<ver>（codex + codex-acp，build 時驗 exact 版本）
  + python3、git、curl、CodeGraph、自家 CLI（company-image、slack-thread-artifact、thread-workspace、sandbox-run）
  − gh（apt purge）
  ✗ 瀏覽器、✗ 附件解析工具

sandbox image（sandbox/Dockerfile）
  FROM SANDBOX_NODE_IMAGE（node bookworm 完整版，帶 digest，對齊前端的 .nvmrc）
  + corepack（依各 repo 的 packageManager 取 pnpm 或 npm）
  + Playwright + Chromium（自己釘的版本，跟 repo 的 Playwright 脫鉤）
  + sandbox-runner、sandbox-screenshot、sandbox-attachment
  以 node 身分跑；需要 apt 的工具改 image，不在 runtime 裝
```

## 執行與持久化

- 一個 OpenAB instance，共用最多 10 個 sessions；閒置 session 4 小時後回收。
- Container 使用者的 uid/gid 由 `HOST_UID`／`HOST_GID` build arg 設定，必須等於執行 docker 的 host 使用者，可寫 bind mount 才成立。sandbox image 的 node uid 固定 1000，所以 `runtime/work`、`runtime/out` 要是 1000:1000。
- Bind mount 帶 `:z`，SELinux enforcing 的 host 才讀得到；`z` 會 relabel 來源目錄，所以 snapshot root 是專用目錄。
- `/home/node` 本身維持唯讀：rootfs 唯讀，只有 `compose.yaml` 列的 mount 可寫。把 home 整層改可寫會讓底下所有唯讀 mount 失去意義。
- bot 可寫的只有 `/home/node/work`、`/home/node/drafts`、`/home/node/code/.index`、`CODEX_HOME` volume 與 OpenAB state（`/home/node/.openab`，bind 到 `runtime/openab`）；`/home/node/out` 唯讀；repo 源碼全部唯讀。
- 每個 snapshot 的 `.codegraph` 是指向 `.index/<repo>` 的相對 symlink。CodeGraph 索引是 WAL 模式的 SQLite，必須可寫；把它移出唯讀樹讓源碼的唯讀保證維持不變。索引由 host 每小時用 runtime image 重建，agent 只能查詢。
- Skill 與 MCP 定義的 canonical catalog 是 public repo `agent-config`（由 skillshare 管理）；其中 `skills/` 整個目錄唯讀掛成 `/home/node/.agents/skills`。container 內不跑 skillshare：可寫的 skill 目錄會讓被 injection 的 thread 植入跨 PM 持續的 skill。catalog 可見不等於 bot 可用；允許清單只在 `agents/CLAUDE.md`（Codex 沒有 skill deny 設定）。
- OpenAB state 與 artifact 暫存檔存在 deployment repo 的 Git-ignored `runtime/`，不隨 container 重建刪除。

## 資源上限與用量告警

host：8 核、31 GB RAM。上限正本 `compose.yaml`，`tests/static.sh` 會比對。

| 項目 | 值 |
|---|---|
| bot | `mem_limit: 6g`、`cpus: 4`、`pids_limit: 1024` |
| 每格 sandbox | `mem_limit: 5g`、`memswap_limit: 5g`（不用 swap）、`cpus: 2`、`pids_limit: 1024` |
| OpenAB `[pool]` | `max_sessions = 10`、`session_ttl_hours = 4` |
| sandbox 磁碟 | 可寫層不設 quota（overlay2 quota 要 xfs pquota，這台不符合）；out 24 小時、每週 `pnpm store prune` |
| `CODEX_HOME` | artifact cleanup 時刪 `sessions/` 底下 mtime 超過 7 天的 rollout；`log/` 與 state sqlite 不動 |
| 用量記錄 | host Caddy JSON access log 保留 14 天：時間、路徑、status、耗時、回應大小。**沒有 thread、使用者、model 欄位**，接受 |

告警 cron（host，每小時 :47，`scripts/alerts.sh` → `scripts/alerts.py`）經 Incoming Webhook 發到維護者
私人 channel，訊息開頭 `[work-agent]`。webhook 是 agent-runner 那個 Slack app 另加的 work-agent 專用
webhook，URL 只在 host 上的檔案；bot 不加入該 channel，bot 的 10 個 scope 不動。

1. 任何 container 被 OOMKilled、RestartCount 增加，或不在跑。
2. 根磁碟可用 < 20%、thread workspace 清完仍 > 20 GB、`CODEX_HOME` > 2 GB；大小超過 3 小時沒回報也告警（清理沒在跑）。
3. `/responses` 上一個完整小時的請求數 > 前 7 天「有請求的小時」中位數的 5 倍（log 不滿 7 天只記錄）。
4. 生圖請求單日（UTC）> 50。

③④ 是初值，上線兩週後用實際 log 調。門檻與細節正本在 `scripts/alerts.py` 檔頭。

## 明確不做

- 不把 GitHub token、SSH key 或 Docker socket 放進 container；不開 privileged；bot image 不裝 `gh`。
- 不把公司 gateway key 放進 container。
- 不使用 claude.ai 的 MCP connectors。
- 不建獨立 broker 或 relay，也不追求 Slack token 隔離。
- 不在 bot container 跑 repo 的 install、dev server 或截圖（約定，不是技術限制）。
- 不做 production build、不跑 repo 自己的 e2e、不開預覽網址。
- 不支援 video，也不解析 ZIP；不做掃描 PDF OCR。
- 不在部署層裁 `agent-config` 的 skill catalog，也不在 container 內跑 skillshare。
- 不讓 workspace agent push、建立 remote、在 snapshot 上 checkout 或寫入 snapshot。
- 不讓 workspace agent 執行 `slack-list ready` 或宣告驗收。
- 不產出 issue 草稿或交接文件，也不發布 GitHub issue。
- 不為每位使用者部署獨立 agent instance；thread 之間不隔離檔案。
- 不支援 Slack slash commands 或 Slack AI assistant mode。

## 驗收情境

1. Slack workspace 使用者能從 app 實際收到的 DM 查 repo；不在 Slack app 事件範圍內的訊息不會觸發 agent。
2. Slack workspace 使用者能在 app 實際收到事件的 channel @ agent，後續在同一 thread 繼續對話；同一 thread 第二次 @ 由 `session/load` 續接，不重開。
3. Slack workspace 使用者從可用 DM 或 channel 明確建立待辦時，新列指派給 sender 並保存來源；同名 active 列不重複建立。
4. 在待辦列的 item 留言串 @ agent 時，agent 能反查正確 `Rec...`，不要求人再貼 ID。
5. 在待辦列 item 留言串詢問該列現況時，agent 在同一串用 Slack 訊息回答，待辦狀態不變。
6. Container 內所有 snapshots 不可寫，`/home/node/work`、drafts 可寫，`/home/node/out` 不可寫；沒有 `gh`、可用的 GitHub auth 或 SSH key；env 裡沒有 `COMPANY_GATEWAY_API_KEY`。
7. Host 同步後，所有新 sessions 讀到同一個基準 branch 版本。
8. `./scripts/deploy.sh` 驗到 container 內的 codex、codex-acp 版本等於 `config/versions.env`，`/etc/openab/config.toml` 的 agent 是 `codex-acp`，`CODEX_HOME` 可寫、上面的 `config.toml` 與 `AGENTS.md` 不可寫。
9. 要改 code 時 agent 呼叫 `thread-workspace`，只在回傳的路徑裡改；同一 thread 再呼叫回同一路徑。由 `tests/thread-workspace.py` 用 mock 驗參數格式、`clone --shared` 與已存在回原路徑。
10. `sandbox-run` 把 job 送進其中一格，sandbox 起 dev server、截圖寫進 `runtime/out/<key>/`，結束後沒有殘留 process；sandbox 寫不進 thread workspace。runner 與 CLI 有 mocked 測試；真的截圖流程只能人工驗。
11. 圖片附件能進到模型；PDF、DOCX、XLSX、PPTX 經 `sandbox-attachment` 轉成 Markdown 後回答；ZIP 與 video 明確回覆不支援。
12. 一般 prototype 得到每個 variant 的截圖；符合條件時得到單一檔案的 HTML prototype；兩者都由受限 uploader 回原 thread。
13. `company-image generate` 經 host proxy 產出 PNG 到 `/home/node/drafts/<UTC 日期>/`；非法 size、走出 drafts 的檔名、允許範圍外的來源圖片都被拒絕且不發出 gateway 請求。靜態部分由 `tests/image-runtime.py` 用假 gateway 驗，真的 gateway 回不回圖要實跑一次。
14. `slack-thread-artifact` 只接受 drafts、work、out 下的 regular PNG、Markdown、HTML；成功依序取得 upload URL、上傳、complete 到 `<sender_context>` 的 channel/thread，只刪 drafts 的檔。`tests/slack-thread-artifact.py` 逐項比對三個請求的 header 與 body；真的 Slack 收不收要實傳一次。
15. 未設定並啟用 STT 時 audio 被明確拒絕；設定獨立相容 endpoint 後才驗 `/audio/transcriptions` 真實轉錄。
16. 兩支上傳／生圖 CLI 與 `thread-workspace` 都走不出固定 root：父目錄被換成 symlink、檔名本身是 symlink、路徑用 `..` 逃逸，都被拒絕。由 `tests/artifact-path-safety.py` 等 mocked 測試重現（回歸測試，不是安全邊界）。
17. sandbox 連不到 host proxy、bot、私有 IP 段與 IMDS；bot 只連得到 sandbox 的 runner port 與 proxy。由 runbook 的拋棄式 container 驗法確認，重開機後仍成立。
18. Deployment host 的 crontab 有 `render_crontab` 產生的 5 個 entry（snapshot 同步、artifact cleanup、thread workspace／out 清理、pnpm prune、告警）。排程存在由 `tests/static.sh` 驗；cron 真的觸發過只能人工確認。
19. `tests/static.sh` 與 `scripts/deploy.sh` 都不聲稱驗過 Slack、公司 gateway、STT 或 cron 觸發；`deploy.sh` 結束時印出未驗清單，對應 [`runbook.md`](runbook.md#人工-release-gate) 的「人工 release gate」。

## 人工前置作業

- Slack app 先 `auth.revoke`／uninstall，再以 10 個 scope 重裝，取得新 `xoxb-...`，邀回 2 個 channel。
- 將 app 顯示名稱改為「派大星教授加博士先生」。
- 把 host 專用 GitHub SSH public key 加入一個能讀 `config/repos.conf` 內所有 private repos 的 GitHub 帳號。
- sudo 安裝 host proxy 與防火牆（`scripts/install-host-network.sh install`），把 gateway key 從 `env/openab.env` 移到 `/etc/work-agent/gateway.env`。
- 在 agent-runner 的 Slack app 另加 work-agent 專用 Incoming Webhook，URL 寫到 host 上的檔案。
- EC2 IMDS hop limit 1＋`http-tokens required`（要有 AWS 憑證的機器）。

實際操作命令見 [`runbook.md`](runbook.md)。
