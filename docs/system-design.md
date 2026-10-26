# Shared Product-Context Bot System Design

## 目標

在 Slack 提供一個多人共用的唯讀 product-context bot，讓授權使用者能直接問產品問題、查待辦、建立指派給自己的待辦，不必經由另一個人轉述。

這個 bot 只回答產品問題。它不修改產品 code、不建立 GitHub issue、不 push，也不通知 PM 驗收。

## 角色

| 角色 | 能做什麼 | 不能做什麼 |
|---|---|---|
| 授權使用者 | 從 Slack 發問、查詢或建立自己的待辦 | 透過遠端 bot 修改 code或取得 GitHub credential |
| product-context bot | 建立 self-assigned Slack 待辦、讀待辦與 repo snapshot、回答產品問題 | 實作、建立 issue、查 private issues、回報驗收 |
| snapshot 同步者 | 在 deployment host 從 GitHub 更新 snapshots | 進入 container或代表 agent發布內容 |
| 實作 agent | 在 local 建 issue、修改 code、測試、push、回報驗收 | 假裝遠端 bot 的回答等於已實作 |

Slack 待辦的回報對象、指派對象，沿用 agent-config `skills/slack-list/SKILL.md` 的定義。

## 系統邊界

```text
授權使用者
  -> Slack DM / private channel / item 留言串
    -> deployment host
      -> 單一 OpenAB container
        -> OpenCode ACP + OMO plugin（預設 agent runtime；可委派 Claude Code ACP specialist）
           -> Slack Lists API
           -> 公司 OpenAI-compatible gateway（模型與生圖）
           -> Cloudflare R2 filestore（incoming attachments）
           -> 唯讀 repo snapshots
          -> 可寫 `/home/node/drafts` 目錄

GitHub
  -> deployment host 的 snapshot 同步者
    -> 唯讀 repo snapshots

local 實作 agent
  -> GitHub issues / 可寫 worktree / push
```

只有一個 container，沒有獨立 broker 或 relay。Slack bot token 與公司 gateway key
都在這個 container 內，由 OpenAB 依 `config/openab.toml` 的 `inherit_env` 傳給 agent process。
這是明確接受的取捨：邊界靠 Slack workspace 與 app 實際收到的事件、唯讀 mount 與 agent runtime 的 permission 設定，
不靠 token 隔離。詳見 [`adr/0007-single-container-opencode-runtime.md`](adr/0007-single-container-opencode-runtime.md)。

模型由 OpenCode 經公司 gateway 提供；主模型、小模型與 OMO role mapping 的唯一設定正本是
[`config/opencode/opencode.json`](../config/opencode/opencode.json) 與
[`config/opencode/oh-my-opencode-slim.json`](../config/opencode/oh-my-opencode-slim.json)。新模型的
本機 context、output、reasoning、tool call 與 image attachment metadata 是待核對的候選值，並非 gateway 實測結果；接入時須依 runbook 確認 gateway 實際 expose 的型號與能力。

GitHub credential只到 deployment host 與 local 實作環境，不跨進 product-context bot container。詳見 [`adr/0001-github-access-stops-at-the-host-boundary.md`](adr/0001-github-access-stops-at-the-host-boundary.md)。

Deployment host是一台 Linux host，Compose以維護者自己的帳號執行。第一版曾把它包在restricted Incus instance內，後來因為該帳號本來就在`docker` group而取消。詳見 [`adr/0003-deploy-directly-on-the-fedora-host.md`](adr/0003-deploy-directly-on-the-fedora-host.md)。

## 互動入口

第一版的 Slack 互動入口是：

- 同一個 Slack workspace 內使用者與 Slack app 的 DM。
- Slack app 實際能收到事件的 channel，以及其中的 thread。

`allow_all_users = true` 讓同一個 Slack workspace 內的使用者在 Slack 事件送達時都能驅動 agent，不再以固定 user ID allowlist 篩選。這不代表任何 workspace 或 channel 都一定可用：實際入口仍受 Slack app 能收到的 DM、channel 與 thread 事件範圍限制；新增 app 能收到事件的 channel 等於擴大互動入口，必須當成權限變更 review。

**已知缺口**：實際部署的 bot token 帶有 `chat:write.public`、`channels:manage`、`groups:write.invites` 等遠多於 [`runbook.md`](runbook.md) 所列的 scope，其中 `chat:write.public` 讓 app 不必被邀請就能貼文到任何公開 channel，「靠邀請維持 channel 邊界」因此不成立。要讓這條邊界成立必須移除多餘 scope 並重新安裝 app（token 會換一組）。

所有能透過 Slack 事件驅動 agent 的 workspace 使用者遠端能力相同，也都拿不到 GitHub credential。

## Repo Snapshots

清單的正本是 [`config/repos.conf`](../config/repos.conf)，一行一個 repo。整個 snapshot root以單一 read-only mount掛成 container內的 `/home/node/code`，所以新增 repo只改那一個檔案，不動 Compose。

Host每小時 fetch所有 remote refs，再把每個 working snapshot reset到基準 branch。所有 OpenAB sessions共用同一份內容。

Snapshot代表最近一次同步成功的狀態，不保證和 GitHub當下完全同步。回答若依賴剛 push的 commit，必須先確認 snapshot同步完成。

product-context bot查符號與影響範圍時用CodeGraph索引，找字串才用grep。索引內容對應snapshot當下的基準branch。

Working tree停在基準 branch，但 `.git`內保有完整的 `origin/*` refs。product-context bot可以用 `git show origin/<branch>:<path>` 這類唯讀方式讀還沒合併的 branch，不能 checkout；回覆引用時必須標明 branch與 commit。

## 待辦流程

### 從 item 留言串開始

1. OpenAB把 sender、`channel_id` 與 `thread_ts` 放進 prompt 的 `<sender_context>`；它不是 ACP side-channel。
2. product-context bot用 `slack-list context` 反查唯一的待辦列。
3. agent讀待辦列、完整 item 留言串與相關 repo snapshots。
4. agent在同一個 item 留言串回答，不改待辦狀態。

### 從 DM 或一般 channel 開始

product-context bot先找對應的既有待辦列；找不到而使用者明確要求建立時，
用`openab.sender.v1`的sender、channel與thread context建立一筆指派給sender的待辦列。標題由agent濃縮時先確認，
使用者明確提供標題時可直接建立；同名active列存在時加入既有assignees，不建立第二列。

`slack-list add`只由這個OpenAB instance執行，local implementation agent不執行，讓同一runtime的process lock
序列化查重與寫入。建立後的來源與回報設定留在原生item留言串；來源註記失敗時保留已建立的列並明確回報，
不直接重試。

### 「我的待辦」

這是多人共用的 agent，不能用固定的個人 ID判斷「我」。必須把當次
`openab.sender.v1.sender_id` 傳給 `slack-list assigned`，精確比對待辦列的 assignee user ID。

## 輸入與輸出邊界

Slack v1 的 native 輸入是 text、image、audio；PDF、DOCX、XLSX、PPTX 另外走 OpenAB 的 R2
文件流程，ZIP 只安全列檔，video 仍直接說明不支援。

輸出限於 Slack 訊息本文與 PNG 圖片產物。
self-contained HTML 只有在使用者明確要求 prototype 或可互動頁面時才輸出，而且必須是單一檔案。
所有產物回到發問的那個 Slack thread；`slack-thread-artifact` 成功上傳才刪暫存，失敗保留交 host cleanup。
「回原 thread」是同一個 container 內的信任約定，不是安全保證，細節見下面「『回原 thread』靠的是什麼」。

Slack 傳進來的圖片要到得了模型，靠的是
[`../config/opencode/opencode.json`](../config/opencode/opencode.json) 裡模型的
`modalities.input` 含 `image`。

OpenAB 的 incoming attachment filestore 使用 Cloudflare R2。兩份 runtime config 的設定完全
相同：bucket `work-agent-attachments`、endpoint
`https://99de68928da234ebcf0c9370443ad7ee.r2.cloudflarestorage.com`、region `auto`、prefix
`incoming/`、presigned URL TTL 3600 秒與單檔上限 **50 MiB**。R2 access key 與 secret key 只
透過 `${R2_ACCESS_KEY_ID}`／`${R2_SECRET_ACCESS_KEY}` interpolation 從未追蹤的
`env/openab.env` 取得，不寫入 repo。

正式文件流程是：OpenAB 收到 PDF／DOCX／XLSX／PPTX → 把 R2 presigned URL 與原始 filename
注入 ACP prompt → agent 原樣執行 `parse-document <url> <filename>` → 取得 Markdown →
根據 Markdown 回答。URL 有效 1 小時；使用者已明確同意它會隨 ACP prompt 傳給 company gateway。
agent 只能使用 OpenAB 注入的 URL，不得使用使用者文字中的 URL、改寫 URL 或自行下載。Compose
的 `PARSE_DOCUMENT_ALLOWED_HOST` 只作 host gate，不是 credential、簽章或 Slack 身分驗證。
ZIP 只用同一個指令取得安全 metadata 檔名清單，不解壓或讀 member 內容；video 仍不支援。

R2 bucket 必須在外部設定 `incoming/` prefix 的 1 天 lifecycle expiration；Slack app 讀取
incoming attachment 需要 `files:read`，更新 scope 後必須 Reinstall to Workspace。preflight 不
嘗試用 Cloudflare 或 Slack API 驗證這兩項，而是要求 interactive checklist；沒有 TTY 的 deploy
安全中止。

圖片**產出**是另一件事，走 `company-image`：一支裝在 image 內的受限 CLI，在同一個 container 完成，
沒有獨立的 artifact broker。它只有 `generate` 與 `edit` 兩個 mode，只收 prompt、允許清單內的 size
與（`edit` 用的）來源圖片路徑；沒有 endpoint、header、model 或輸出路徑參數，gateway 位置與 key
只從 runtime env 讀。請求固定用公司 gateway 的 Responses API `image_generation` tool，輸出固定是
PNG，固定寫到 `/home/node/drafts/<UTC 日期>/`。回應的兩種形狀都吃：一般 JSON body 與
`text/event-stream` 的 SSE（實測公司 gateway 回的是後者，圖在 `partial_image_b64`）——
用回應的 Content-Type 決定怎麼讀，兩條路徑都要過同一組 base64 解碼與 PNG magic 檢查才寫檔，
所以 gateway 哪天改回非 stream 也不會壞。介面就是能力上限：多一個尺寸都要改 repo 重新
build。理由與拒絕的替代方案見
[`adr/0008-restricted-company-image-cli.md`](adr/0008-restricted-company-image-cli.md)。

單一 agent runtime 共用這一支 —— image 由同一份 `Dockerfile` 建置，唯一 OpenAB config 都把
`COMPANY_GATEWAY_*` 放進 `inherit_env`。所以「Claude ACP 是可用的 rollback」這句話在生圖上也成立，
不是只在文件上成立。

產圖後用 `slack-thread-artifact` 回原 thread。它固定執行 `files.getUploadURLExternal → upload →
files.completeUploadExternal`，只讀 drafts 下的 regular PNG、Markdown、HTML，且成功才刪檔。

兩個 Slack Web API 呼叫的 body 是 `application/x-www-form-urlencoded`，不是 JSON —— JSON body 會被
`files.getUploadURLExternal` 回 `invalid_arguments`，bot 就一個檔案都傳不出去。`complete` 的 `files`
在 form 裡是一個 JSON 字串欄位。中間上傳 bytes 的那一段不是 Web API：只用 Slack 回的 upload URL、
artifact mime 與原始 bytes，不帶 token。細節見
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

CLI 的價值只在縮小可造成的副作用：拿掉任意 URL、header、命令與輸出路徑的自由度，並且
把檔案來源鎖在 drafts。這不是把 prompt context 變成安全邊界。

刻意**不**改成 broker 或 relay：那要另一個 process 持有 token 並自己判斷 thread 歸屬，而
判斷依據仍然只有 OpenAB 給的同一份 context，換不到新的保證，只多一層要維護的東西。完整取捨見
[`adr/0009-thread-artifact-upload-and-optional-stt.md`](adr/0009-thread-artifact-upload-and-optional-stt.md)
與 [`adr/0007-single-container-opencode-runtime.md`](adr/0007-single-container-opencode-runtime.md)。

上傳失敗時 artifact 暫存檔保留，由 deployment host 的 `# work-agent-artifact-cleanup` crontab entry
（`scripts/cleanup-artifacts.sh` → container 內 `slack-list cleanup`，每小時 :17）收掉；保留期限
24 小時，檢查每小時一次，所以實際壽命是 24 到 25 小時。

Audio 只有獨立的 OpenAI-compatible STT endpoint 已被確認、設定 `STT_BASE_URL`／`STT_API_KEY` 並把兩份
OpenAB config 的 `[stt].enabled` 改成 `true` 時才可轉錄。OpenAB 固定使用該 base URL 的
`/audio/transcriptions`；不假設 company gateway 支援。預設未啟用時 bot 明確拒絕 audio。

## 對話回覆

一般 Slack 對話先回答產品結論、使用者目前會遇到什麼，以及期望改成什麼。除非授權使用者明確追問，否則不在對話回覆附檔名、行號、函式名、state、payload 或 API。

Runtime或部署故障造成工具不能執行時，product-context bot只告知哪項查詢暫時不可用，並指出需要部署維護者修復，不要求 Slack 使用者執行 container排障或安裝套件。

## Agent Runtime 與版本

預設 agent runtime 是 OpenCode 的原生 ACP（`opencode acp`），加上 oh-my-opencode-slim（OMO）
plugin。OMO 由 Sol 統籌、Luna 負責查詢與執行、Astra 負責深入分析；精確角色 mapping 以
[`../config/opencode/oh-my-opencode-slim.json`](../config/opencode/oh-my-opencode-slim.json) 為準。
`acpAgents.claude-code` 是由 OMO 依任務自行決定是否委派的 Claude Code ACP specialist，設定在同一份檔案。

Claude specialist 的 ACP adapter 是 image 內由 Docker pin 安裝的 `claude-agent-acp`，不是 runtime
`npx` download；Claude Code CLI 也在 image 內固定版本。它與 Claude rollback 共用
`claude-credentials` named volume。首次需要時執行一次 `claude auth login`，之後由 OMO 依任務 routing
決定是否透過 wrapper 委派。

Slack 不需要特殊 prefix，也沒有 prefix 強制委派規則。OMO Slim v2.2.15 使用內建 autonomous routing，
依任務自行決定是否透過 `@claude-code` ACP wrapper 委派 Claude Code；這是 LLM routing，非 deterministic，
不保證每個複雜工作都會交給 Claude Code。orchestrator 不直接啟動 ACP；wrapper 失敗時明確回報委派失敗，
不得靜默 fallback 到本地處理。

OpenAB 只有一個 deployment runtime：`config/openab.toml` 的 `opencode acp`。Claude ACP adapter 與 Claude Code CLI 仍由 Docker 以固定版本安裝，僅作 OMO specialist，不作 OpenAB rollback。

所有版本與 OpenAB immutable image digest 集中在 `config/versions.env`；`compose.yaml` 使用 checked-in build args，
因此 `docker compose` 可直接 render。Dockerfile 會在 build 時驗證 base image 內的 OpenCode 版本等於 `OPENCODE_VERSION`。

`managed-claude-settings.json` 是 specialist 使用的設定檔，唯讀掛載到 `/home/node/.claude/settings.json`。

## 執行與持久化

- 一個 OpenAB instance，共用最多 10 個 sessions。
- 閒置 session 4 小時後回收。
- Claude Code login存在獨立 credential volume；OpenCode 的 auth、session storage 與 plugin
  cache 各有一個 named volume（`opencode-data`、`opencode-cache`），因為 rootfs 是唯讀的。
- `/home/node/.config/opencode` 是可寫的 named volume（`opencode-config`），`config/opencode`
  內的每個設定檔以**單檔唯讀** mount 疊在上面。OpenCode 會在自己的 config dir 寫 `.gitignore`
  與 state，整個目錄唯讀時 `opencode acp` 起不來；單檔唯讀讓 agent 仍然改不掉 provider 與
  permission 設定。OMO plugin 由 image 提供並固定版本，runtime 不上網抓 plugin，`autoUpdate`
  與 OpenCode `autoupdate` 都關掉。
- OpenCode 另外有一個 state root `/home/node/.opencode`，也會在裡面寫 `.gitignore` 與 state。
  rootfs 唯讀而這個路徑沒有 mount 時，`opencode acp` 會用跟上面同一種方式在啟動時死掉。它有
  自己的 named volume（`opencode-state`）。這是**兩個**不同的目錄，修好 config dir 不會順便
  修好它。`/home/node` 本身維持唯讀：把 home 整層改可寫會讓底下所有唯讀 mount 失去意義。
- `/home/node/.opencode`（OpenCode state）與 `/home/node/.openab`（OpenAB state，bind 到
  `runtime/openab`）是不同程式的不同目錄，只差一個字，讀 compose 時不要看混。
- Container使用者的 uid/gid由 `HOST_UID`／`HOST_GID` build arg設定，必須等於執行 docker的 host使用者，可寫 bind mount才成立。
- Bind mount帶 `:z`，SELinux enforcing的 host才讀得到；`z` 會 relabel來源目錄，所以 snapshot root是專用目錄。
- Skill 與 MCP 定義的 canonical catalog 是 public repo `agent-config`（由 skillshare 管理）；其中 `skills/`
  整個目錄唯讀掛成 `/home/node/.claude/skills`，OpenCode 再由
  `config/opencode/opencode.json` 的 `skills.paths` 指向它。catalog 可見不等於 OpenCode permission allow，
  也不等於這個 runtime contract 允許 bot 使用；實際 authorization 仍由本 repo 的 permission、OMO/runtime
  設定與 `agents/CLAUDE.md` 決定。同步新 catalog entry 不會自動新增 bot 能力；要新增能力必須 review
  permission、runtime contract 與 Slack Home，且不在 deployment repo 另抄一份 skill 清單。
- OpenAB state與 artifact 暫存檔存在 deployment repo的 Git-ignored `runtime/`，不隨 container重建刪除。
- Project資料中可寫的只有 `/home/node/drafts` 與 `/home/node/code/.index`；repo源碼全部唯讀。
- 每個 snapshot 的 `.codegraph` 是指向 `.index/<repo>` 的相對 symlink。CodeGraph索引是WAL模式的SQLite，必須可寫；把它移出唯讀樹讓源碼的唯讀保證維持不變。索引由host每小時用runtime image重建，agent只能查詢。
- OpenAB image使用固定 multi-arch digest，不跟浮動 tag更新。
- Runtime image從固定 digest的 OpenAB image建置，額外提供 `slack-list` 所需的 Python 3、
  唯讀 git 偵察所需的 git（`-opencode` base image 沒有）、固定版本的 CodeGraph 與 OMO，以及
  生圖用的 `company-image`。
- `company-image` 由 root 擁有、mode 0755，agent 只能執行不能改寫；build 時會跑一次
  `company-image --help`，語法或 Python 版本不合會 build 失敗。

## 明確不做

- 不把 GitHub token、SSH key或 Docker socket放進 container。
- 不使用 claude.ai 的 MCP connectors。它們跟著登入帳號同步進 container；這個 deployment 不掛入 connectors。
- 不建獨立 broker 或 relay，也不追求 Slack token 隔離。
- 不支援 video，也不解壓或解析 ZIP 內容；PDF、DOCX、XLSX、PPTX 只走 OpenAB 提供的 R2 URL
  與固定的 `parse-document` 流程。
- 不在部署層裁 `agent-config` 的 skill catalog。
- 不讓 product-context bot clone、fetch、建立 branch/worktree、commit或 push。
- 不讓 product-context bot執行 `slack-list ready` 或宣告驗收。
- 不產出 issue 草稿或交接文件（2026-09-26 移除），也不發布 GitHub issue。
- 不為每位授權使用者部署獨立 agent instance。
- 第一版不支援 Slack slash commands或 Slack AI assistant mode。

## 驗收情境

1. Slack workspace 使用者能從 app 實際收到的 DM 查 repo；不在 Slack app 事件範圍內的訊息不會觸發 agent。
2. Slack workspace 使用者能在 app 實際收到事件的 channel @ agent，後續在同一 thread 繼續對話。
3. Slack workspace 使用者從可用 DM 或 channel 明確建立待辦時，新列指派給 sender 並保存來源；同名 active 列不重複建立。
4. 在待辦列的 item 留言串 @ agent時，agent能反查正確 `Rec...`，不要求人再貼 ID。
5. 在待辦列 item 留言串詢問該列現況時，agent 在同一串用 Slack 訊息回答，待辦狀態不變。
6. Container內所有 snapshots不可寫，drafts可寫，且沒有可用的 GitHub auth或 SSH key。
7. Host同步後，所有新 sessions讀到同一個基準 branch版本。
8. `./scripts/deploy.sh` 在 `固定的 OpenCode runtime` 下能驗到 container 內的
   `opencode` 版本等於 `OPENCODE_VERSION`、OMO 版本等於 `OMO_VERSION`，且
   `/etc/openab/config.toml` 的 agent 是 `opencode acp`。
9. 把 `固定的 OpenCode runtime` 改成 `claude` 後，同一支 `deploy.sh` 改為驗
   `claude-agent-acp` 存在且 config 指向它，Slack workspace user access 與事件範圍設定不變。
10. 圖片附件能進到模型；PDF、DOCX、XLSX、PPTX 會經 `parse-document <url> <filename>` 轉成
    Markdown 後回答；ZIP 只列檔，video 明確回覆不支援。
11. 一般請求得到 PNG；明確要求 prototype 時才得到單一檔案的 HTML；兩者都由受限 uploader 回原 thread。
12. `company-image generate` 在 container 內產出 PNG 到 `/home/node/drafts/<UTC 日期>/`；
    非法 size、走出 drafts 的檔名、允許範圍外的來源圖片都被拒絕且不發出 gateway 請求。
    這一項的靜態部分由 `tests/image-runtime.py` 用假 gateway 驗，真的 gateway 回得回圖要在
    有 key 的環境實跑一次。
13. 把 `固定的 OpenCode runtime` 改成 `claude` 後，同一支 `company-image` 仍在，
     `COMPANY_GATEWAY_*` 仍傳得到 agent process。
14. `slack-thread-artifact` 只接受 drafts 下的 regular PNG、Markdown、HTML；成功依序取得 upload URL、上傳、
    complete 到 `<sender_context>` 的 channel/thread 後刪檔。失敗不刪檔，且 Claude rollback 同樣可執行。
    兩個 Slack Web API 呼叫送 form body（`complete` 的 `files` 是 JSON 字串欄位），bytes 那一段用
    Slack 回的 URL 與 artifact mime 且不帶 token。`tests/slack-thread-artifact.py` 逐項比對三個請求的
    header 與 body；真的 Slack 收不收要在有 token 的環境實傳一次。
15. 未設定並啟用 STT 時 audio 被明確拒絕；設定獨立相容 endpoint 後才驗 `/audio/transcriptions` 真實轉錄。
16. 兩支 CLI 都走不出固定 root：父目錄被換成 symlink、檔名本身是 symlink、路徑用 `..` 逃逸，
    都被拒絕且不發出任何 Slack 或 gateway 請求。`company-image` 的目標檔名被事先種成
    指向外面的 symlink 時，寫到下一個可用檔名，不寫穿它。由 `tests/artifact-path-safety.py` 重現。
17. 上傳成功後暫存檔被刪；但暫存檔在讀取之後被換成別的檔案或 symlink 時，**不刪**那個東西，
    在 stderr 說明並仍 exit 0（檔案確實送出去了，回非零只會讓 agent 重傳）。
18. Deployment host 的 crontab 有 `# work-agent-artifact-cleanup` entry，每小時執行
    `scripts/cleanup-artifacts.sh`，它在 container 內跑 `slack-list cleanup`；container 沒在跑時
    這一輪失敗會寫進 `artifact-cleanup.log` 並回非零。排程存在由 `tests/static.sh` render
    `render_crontab` 驗；cron 真的觸發過只能人工確認。
19. `tests/static.sh` 與 `scripts/deploy.sh` 都不聲稱驗過 Slack、公司 gateway、STT 或 cron 觸發；
    `deploy.sh` 結束時印出未驗清單，對應 [`runbook.md`](runbook.md#人工-release-gate) 的
    「人工 release gate」。

## 人工前置作業

- Slack app重新安裝並取得 `xapp-...`、新 `xoxb-...`。
- 將 app顯示名稱改為「派大星教授加博士先生」。
- 把 host專用 GitHub SSH public key加入一個能讀 `config/repos.conf` 內所有 private repos的 GitHub帳號。
- 首次需要時在 container 內執行一次 `claude auth login`。

實際操作命令見 [`runbook.md`](runbook.md)。
