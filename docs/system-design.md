# Company Agent System Design

## 目標

在 Slack 提供一個公司同事共用的 coding agent。它拿 repo snapshot 當 product context 回答產品問題、查詢或建立待辦；需要時 clone 一份 repo 到自己的 home 修改、安裝、跑測試或 dev server，再把結果（說明、patch、截圖、檔案）交回原 Slack thread。

它送不回 GitHub：container 裡沒有 GitHub credential，不 push、不開 PR／issue，也不通知 PM 驗收。改好的 code 是 `.patch`，由人在自己的電腦套用與 push。

整體形狀照 OpenAB 官方的 Codex 部署，理由見 [`adr/0012-follow-the-official-openab-codex-deployment.md`](adr/0012-follow-the-official-openab-codex-deployment.md)。

## 角色

| 角色 | 能做什麼 | 不能做什麼 |
|---|---|---|
| Slack 使用者 | 從 Slack 發問、查詢或建立自己的待辦、請 agent 改 code 並拿回 patch | 透過 agent 取得 GitHub credential 或推到 GitHub |
| company agent | 讀 repo snapshot、clone 到 home 修改與執行、讀寫 Slack 待辦、上傳 PNG／patch 等產物 | push、建立 issue、查 private issues、回報驗收 |
| snapshot 同步者 | 在 deployment host 從 GitHub 更新 snapshots | 進入 container 或代表 agent 發布內容 |
| 實作 agent／人 | 在 local 套用 patch、review、測試、push、回報驗收 | 把 agent 的 patch 當成已上線 |

Slack 待辦的回報對象、指派對象，沿用 agent-config `skills/slack-list/SKILL.md` 的定義。

## 系統邊界

```text
Slack 使用者
  -> Slack DM / channel / item 留言串
    -> deployment host
      -> 單一 container（OpenAB + codex-acp）
         -> Slack Web API / Lists API
         -> 公司 gateway 的 Responses API（模型與生圖）
         -> Cloudflare R2 filestore（incoming attachments）
         -> /home/node/snapshots（唯讀 repo snapshots）
         -> /home/node（可寫 home：工作副本、套件、drafts、Codex session）

GitHub
  -> deployment host 的 snapshot 同步者
    -> 唯讀 repo snapshots

人／local 實作 agent
  -> 套用 patch / push / GitHub issues
```

只有一個 container，沒有 broker、relay 或 proxy。Slack bot token 與公司 gateway key 都在這個 container 內，由 OpenAB 依 [`config/openab.toml`](../config/openab.toml) 的 `inherit_env` 傳給 agent process；agent 執行的 repo code（含第三方套件的 install script）也跑在同一個 container。這是照 OpenAB 官方部署接受的取捨，這個 bot 只給公司內部使用；開放給公司外的人之前要重新決定。不宣稱任何 token 隔離。

模型由 Codex 直連公司 gateway 的 Responses API（`<COMPANY_GATEWAY_BASE_URL>/responses`）。provider 與模型的正本是 [`config/codex/config.toml`](../config/codex/config.toml)，它是範本：Codex 的 provider 設定不讀 env，所以 `scripts/deploy.sh` 把 `COMPANY_GATEWAY_BASE_URL` 填進去，寫成 `runtime/codex/config.toml` 再唯讀 mount；key 由 `env_key` 從 env 讀。gateway 只提供 Responses API 的理由見 [`adr/0010-company-provider-uses-the-responses-api.md`](adr/0010-company-provider-uses-the-responses-api.md)。

GitHub credential 只到 deployment host 與 local 實作環境，不進 container。詳見 [`adr/0001-github-access-stops-at-the-host-boundary.md`](adr/0001-github-access-stops-at-the-host-boundary.md)。

Deployment host 是一台 Linux host，Compose 以維護者自己的帳號執行。詳見 [`adr/0003-deploy-directly-on-the-fedora-host.md`](adr/0003-deploy-directly-on-the-fedora-host.md)。

## 互動入口

第一版的 Slack 互動入口是：

- 同一個 Slack workspace 內使用者與 Slack app 的 DM。
- Slack app 實際能收到事件的 channel，以及其中的 thread。

`allow_all_users = true` 讓同一個 Slack workspace 內的使用者在 Slack 事件送達時都能驅動 agent，不再以固定 user ID allowlist 篩選。這不代表任何 workspace 或 channel 都一定可用：實際入口仍受 Slack app 能收到的 DM、channel 與 thread 事件範圍限制；新增 app 能收到事件的 channel 等於擴大互動入口，必須當成權限變更 review。

**已知缺口**：實際部署的 bot token 帶有 `chat:write.public`、`channels:manage`、`groups:write.invites` 等遠多於 [`runbook.md`](runbook.md) 所列的 scope，其中 `chat:write.public` 讓 app 不必被邀請就能貼文到任何公開 channel，「靠邀請維持 channel 邊界」因此不成立。要讓這條邊界成立必須移除多餘 scope 並重新安裝 app（token 會換一組）。

所有能透過 Slack 事件驅動 agent 的 workspace 使用者遠端能力相同，也都拿不到 GitHub credential。

## Repo Snapshots

清單的正本是 [`config/repos.conf`](../config/repos.conf)，一行一個 repo。整個 snapshot root 以單一 read-only mount 掛成 container 內的 `/home/node/snapshots`，所以新增 repo 只改那一個檔案，不動 Compose。

Host 每小時 fetch 所有 remote refs，再把每個 snapshot reset 到基準 branch。所有 session 共用同一份內容。snapshot 唯讀是為了正確性（所有 thread 共用、每小時會被同步蓋掉），不是安全邊界。

Snapshot 代表最近一次同步成功的狀態，不保證和 GitHub 當下完全同步。回答若依賴剛 push 的 commit，必須先確認 snapshot 同步完成。

agent 查符號與影響範圍時用 CodeGraph 索引，找字串才用 grep。索引內容對應 snapshot 當下的基準 branch。

Working tree 停在基準 branch，但 `.git` 內保有完整的 `origin/*` refs。agent 可以用 `git show origin/<branch>:<path>` 讀還沒合併的 branch；回覆引用時必須標明 branch 與 commit。要在別的 branch 上動手就先 clone 到 home 再切，不在 snapshot 上 checkout。

## Coding 流程

1. agent 把 snapshot clone 到 home：`git clone /home/node/snapshots/<repo> /home/node/<目錄>/<repo>`。
2. 在副本裡改 code、用 pnpm／npm 安裝套件、跑測試或 dev server。工具都裝在 home；image 不給 `apt`，缺系統套件要由部署維護者加進 image。
3. dev server 只有 container 內看得到，要給人看就截圖回 thread。預覽網址（內網穿透）之後另做。
4. 改好的 code 用 `git format-patch` 寫進 `/home/node/drafts/<UTC 日期>/`，再用 `slack-thread-artifact` 上傳到原 thread。
5. 人在自己的電腦套用 patch、review、push。agent 不說「已修好」「已上線」。

home 是所有 thread 共用的空間，沒有自動清理；clone 與套件快取會累積，volume 變大時再加清理。

## 待辦流程

### 從 item 留言串開始

1. OpenAB 把 sender、`channel_id` 與 `thread_ts` 放進 prompt 的 `<sender_context>`；它不是 ACP side-channel。
2. agent 用 `slack-list context` 反查唯一的待辦列。
3. agent 讀待辦列、完整 item 留言串與相關 repo snapshots。
4. agent 在同一個 item 留言串回答，不改待辦狀態。

### 從 DM 或一般 channel 開始

agent 先找對應的既有待辦列；找不到而使用者明確要求建立時，用 `openab.sender.v1` 的 sender、channel 與 thread context 建立一筆指派給 sender 的待辦列。標題由 agent 濃縮時先確認，使用者明確提供標題時可直接建立；同名 active 列存在時加入既有 assignees，不建立第二列。

`slack-list add` 只由這個 OpenAB instance 執行，local implementation agent 不執行，讓同一 runtime 的 process lock 序列化查重與寫入。建立後的來源與回報設定留在原生 item 留言串；來源註記失敗時保留已建立的列並明確回報，不直接重試。

### 「我的待辦」

這是多人共用的 agent，不能用固定的個人 ID 判斷「我」。必須把當次 `openab.sender.v1.sender_id` 傳給 `slack-list`，精確比對待辦列的 assignee user ID。

## 輸入與輸出邊界

Slack 的 native 輸入是 text、image，以及啟用 STT 時的 audio。PDF、DOCX、XLSX、PPTX 這類附件走 OpenAB 的 R2 filestore：OpenAB 把 R2 `incoming/` 的 presigned URL 與原始 filename 放進 prompt，agent 自己下載、轉成文字再回答；需要轉檔工具就裝在 home。轉不出來（例如需要 OCR 的掃描檔）就直說，不猜內容。agent 只下載 OpenAB 放進 prompt 的那個 URL，不把它傳給其他服務。presigned URL 會隨 prompt 送到公司 gateway。

R2 filestore 的設定正本是 `config/openab.toml` 的 `[filestore]`。R2 access key 與 secret key 只透過 `${R2_ACCESS_KEY_ID}`／`${R2_SECRET_ACCESS_KEY}` interpolation 從未追蹤的 `env/openab.env` 取得，不寫入 repo。R2 bucket 必須在外部設定 `incoming/` prefix 的 1 天 lifecycle expiration；Slack app 讀取 incoming attachment 需要 `files:read`，更新 scope 後必須 Reinstall to Workspace。preflight 不嘗試用 Cloudflare 或 Slack API 驗證這兩項，而是要求 interactive checklist；沒有 TTY 的 deploy 安全中止。

輸出是 Slack 訊息本文、PNG、`.patch` 與 Markdown 檔。self-contained HTML 只有在使用者明確要求 prototype 或可互動頁面時才輸出，而且必須是單一檔案。所有產物回到發問的那個 Slack thread；`slack-thread-artifact` 成功上傳才刪暫存，失敗保留交 host cleanup。「回原 thread」是同一個 container 內的信任約定，不是安全保證，細節見下面「『回原 thread』靠的是什麼」。

圖片**產出**走 `company-image`：一支裝在 image 內的受限 CLI，在同一個 container 完成，沒有獨立的 artifact broker。它只有 `generate` 與 `edit` 兩個 mode，只收 prompt、允許清單內的 size 與（`edit` 用的）來源圖片路徑；沒有 endpoint、header、model 或輸出路徑參數，gateway 位置與 key 只從 runtime env 讀。請求固定用公司 gateway 的 Responses API `image_generation` tool，輸出固定是 PNG，固定寫到 `/home/node/drafts/<UTC 日期>/`。回應的兩種形狀都吃：一般 JSON body 與 `text/event-stream` 的 SSE（實測公司 gateway 回的是後者，圖在 `partial_image_b64`）—— 用回應的 Content-Type 決定怎麼讀，兩條路徑都要過同一組 base64 解碼與 PNG magic 檢查才寫檔。介面就是能力上限：多一個尺寸都要改 repo 重新 build。agent 被要求只用這一支產圖，不自己打 gateway。理由與拒絕的替代方案見 [`adr/0008-restricted-company-image-cli.md`](adr/0008-restricted-company-image-cli.md)。

產物用 `slack-thread-artifact` 回原 thread。它固定執行 `files.getUploadURLExternal → upload → files.completeUploadExternal`，只讀 drafts 下的 regular `.png`、`.md`、`.html`、`.patch`，且成功才刪檔。

兩個 Slack Web API 呼叫的 body 是 `application/x-www-form-urlencoded`，不是 JSON —— JSON body 會被 `files.getUploadURLExternal` 回 `invalid_arguments`，bot 就一個檔案都傳不出去。`complete` 的 `files` 在 form 裡是一個 JSON 字串欄位。中間上傳 bytes 的那一段不是 Web API：只用 Slack 回的 upload URL、artifact mime 與原始 bytes，不帶 token。細節見 [`adr/0009`](adr/0009-thread-artifact-upload-and-optional-stt.md)。

### 「回原 thread」靠的是什麼

**這是信任約定，不是安全保證。** 說清楚它由什麼組成：

- OpenAB 把當次的 sender、`channel_id`、`thread_ts` 寫進 prompt 裡的 `<sender_context>`。那是 **prompt content**，不是 ACP side-channel，也不是不可竄改的授權資料。
- agent 讀那段文字，再把 `--channel`、`--thread-ts` 逐字交給 CLI。這一步由 `agents/AGENTS.md` 的文字約束，不由任何機制強制。
- CLI 只檢查格式（`C/D/G` 開頭的 channel ID、Slack timestamp），不能判斷這組值是不是本次 sender 真正的 thread。
- 三者在**同一個 container** 內，共用同一個 `SLACK_BOT_TOKEN`。

所以能被信任的原因是「這三段都在同一個信任域內」，不是任何一段驗證了另一段。**不宣稱** token isolation、cryptographic binding，也**不宣稱**擋得住 prompt injection：能改變 agent 送出什麼值的輸入，就能改變檔案送到哪個 thread（限於這個 bot token 本來就進得去的 conversation）。agent 讀得到 token，本來就能繞過 CLI。

CLI 的價值只在縮小正常操作下的副作用：拿掉任意 URL、header、命令與輸出路徑的自由度，並且把檔案來源鎖在 drafts。這不是把 prompt context 變成安全邊界。

刻意**不**改成 broker 或 relay：那要另一個 process 持有 token 並自己判斷 thread 歸屬，而判斷依據仍然只有 OpenAB 給的同一份 context，換不到新的保證，只多一層要維護的東西。完整取捨見 [`adr/0009-thread-artifact-upload-and-optional-stt.md`](adr/0009-thread-artifact-upload-and-optional-stt.md)。

上傳失敗時 artifact 暫存檔保留，由 deployment host 的 `# agent-company-artifact-cleanup` crontab entry（`scripts/cleanup-artifacts.sh` → container 內 `slack-list cleanup`，每小時 :17）收掉；保留期限 24 小時，檢查每小時一次，所以實際壽命是 24 到 25 小時。

Audio 只有獨立的 OpenAI-compatible STT endpoint 已被確認、設定 `STT_BASE_URL`／`STT_API_KEY` 並把 `config/openab.toml` 的 `[stt].enabled` 改成 `true` 時才可轉錄。OpenAB 固定使用該 base URL 的 `/audio/transcriptions`；不假設 company gateway 支援。預設未啟用時 bot 明確拒絕 audio。

## 對話回覆

一般 Slack 對話先回答產品結論、使用者目前會遇到什麼，以及期望改成什麼。除非使用者明確追問，否則不在對話回覆附檔名、行號、函式名、state、payload 或 API。

Runtime 或部署故障造成工具不能執行時，agent 只告知哪項查詢暫時不可用，並指出需要部署維護者修復，不要求 Slack 使用者執行 container 排障或安裝套件。

## Agent Runtime 與版本

OpenAB 直接跑 `codex-acp`，初始 mode 是 `agent-full-access`。這是 OpenAB 官方對 container 部署的建議：container 內建不了 sandbox namespace，其他 mode 的 shell 不是失敗就是每條都要人按允許；邊界是 container 本身。`CODEX_HOME`、`CODEX_PATH` 與 mode 的設定在 [`config/openab.toml`](../config/openab.toml) 的 `[agent]`，每一項的理由寫在該檔註解。

Codex CLI 與 codex-acp 由 Dockerfile 以固定版本裝進 `/opt/codex-<版本>`，build 時驗 exact version；上游 image 自帶的那兩個不用。Codex 的 model catalog 是 [`config/codex/catalog.json`](../config/codex/catalog.json)，升級 Codex 時要重產，parse 失敗 Codex 整個起不來。

所有版本與 OpenAB immutable image digest 集中在 [`config/versions.env`](../config/versions.env)；`compose.yaml` 使用 checked-in build args，因此 `docker compose` 可直接 render。

## 執行與持久化

- 一個 OpenAB instance，共用最多 10 個 session，閒置 4 小時後回收。
- `/home/node` 是 named volume `home`，可寫、container 重建後還在，`working_dir` 也是它（官方用 PVC）。Codex session（`CODEX_HOME=/home/node/.codex`）、OpenAB state（`/home/node/.openab`）、`/home/node/drafts`、agent clone 的工作副本與自己裝的工具都在裡面。rootfs 不是唯讀。
- 疊在 home 上的唯讀 mount（正本見 [`compose.yaml`](../compose.yaml)）：
  - `/home/node/snapshots`：整個 snapshot root。
  - `/etc/codex/config.toml`（deploy 產生的 provider）、`/etc/codex/AGENTS.bot.md`（`agents/AGENTS.md`）、`~/.config/skillshare/config.yaml`：agent-sync 的輸入。
  - `/etc/codex/catalog.json` 與 `/etc/openab/config.toml`。
- `~/.codex/config.toml`、`~/.codex/AGENTS.md`、`~/.agents/skills` 由 [`agents/bin/agent-sync`](../agents/bin/agent-sync) 在 OpenAB `pre_boot` 與每小時 snapshot 同步後產生，跟維護者機器的 chezmoi + skillshare 同一套：provider 換新、skillshare 寫的 MCP 條目保留（整份覆蓋會讓 skillshare 拒絕同步），規則是維護者個人規則接上 bot 專屬規則。這幾個檔在 home 裡、agent 改得到；agent 本來就拿得到 gateway key，這是 ADR 0012 接受的取捨。
- `chrome` sidecar 跑固定 digest 的 headless Chrome，透過只持有網路的 `net`（k8s pause container）跟 agent 共用 localhost，任一邊重啟都不影響另一邊：agent 的 playwright-cli（`attach --cdp`）與 chrome-devtools MCP 都連 `127.0.0.1:9222`（跟維護者機器同一個 `--browser-url`），Chrome 開得到 agent 跑起來的 dev server。sidecar 沒有 token 與 volume，只聽 127.0.0.1。使用者連不到它；給人看的預覽網址之後另做。
- `/home/node/snapshots/.index` 是唯一可寫的 snapshot 相關路徑。每個 snapshot 的 `.codegraph` 是指向 `.index/<repo>` 的相對 symlink；CodeGraph 索引是 WAL 模式的 SQLite，必須可寫。索引由 host 每小時用 runtime image 重建，agent 只能查詢。
- Container 使用者的 uid/gid 由 `HOST_UID`／`HOST_GID` build arg 設定，必須等於執行 docker 的 host 使用者，可寫 bind mount 才成立。
- Bind mount 帶 `:z`，SELinux enforcing 的 host 才讀得到；`z` 會 relabel 來源目錄，所以 snapshot root 是專用目錄。
- Container 有 CPU、記憶體與 pids 上限（見 `compose.yaml`），agent 跑爆時只殺自己，不拖垮 host。
- Skill、MCP 與個人規則的正本是 public repo `agent-config`（由 skillshare 管理），bot 跟維護者機器用同一份，不另裁。在 agent-config 新增的 skill 或 MCP，最晚一小時後 bot 也有；需要這個環境沒有的工具或權限（GitHub 寫入、herdr）的 skill 在這裡會失敗。bot 專屬的限制寫在 `agents/AGENTS.md`，改能力時一起看 Slack Home。
- OpenAB image 使用固定 multi-arch digest，不跟浮動 tag 更新。
- `company-image` 與 `slack-thread-artifact` 由 root 擁有、mode 0755，agent 只能執行不能改寫；build 時各跑一次 `--help`，語法或 Python 版本不合會 build 失敗。

## 明確不做

- 不把 GitHub token、SSH key 或 Docker socket 放進 container；agent 不 push、不開 PR、不建 GitHub issue。
- 不使用 claude.ai 的 MCP connectors。
- 不建獨立 broker、relay 或 gateway proxy，也不追求 Slack token 或 gateway key 隔離。
- 不在 container 內另做 sandbox 或每個 thread 一份工作副本的隔離。
- 不提供預覽網址；dev server 只在 container 內。
- 不在部署層裁 `agent-config` 的 skill catalog。
- 不讓 agent 執行 `slack-list ready` 或宣告驗收。
- 不產出 issue 草稿或交接文件，也不發布 GitHub issue。
- 不為每位使用者部署獨立 agent instance。
- 第一版不支援 Slack slash commands 或 Slack AI assistant mode。

## 驗收情境

1. Slack workspace 使用者能從 app 實際收到的 DM 查 repo；不在 Slack app 事件範圍內的訊息不會觸發 agent。
2. Slack workspace 使用者能在 app 實際收到事件的 channel @ agent，後續在同一 thread 繼續對話；第二次 @ 時恢復同一個 session（`session/load`），不是重開。
3. Slack workspace 使用者從可用 DM 或 channel 明確建立待辦時，新列指派給 sender 並保存來源；同名 active 列不重複建立。
4. 在待辦列的 item 留言串 @ agent 時，agent 能反查正確 `Rec...`，不要求人再貼 ID。
5. 在待辦列 item 留言串詢問該列現況時，agent 在同一串用 Slack 訊息回答，待辦狀態不變。
6. Container 內 home 可寫、所有 snapshots 與 skills 不可寫、`.index` 可寫，且沒有可用的 GitHub auth 或 SSH key。
7. Host 同步後，所有新 session 讀到同一個基準 branch 版本。
8. `./scripts/deploy.sh` 驗到 PATH 上的 `codex-acp` 與 `CODEX_PATH` 指向 `/opt/codex-<CODEX_VERSION>`、兩者版本等於 `config/versions.env`、`/etc/openab/config.toml` 的 agent 是 `codex-acp`、Codex provider 設定已填入 gateway 位址且唯讀，退場的 runtime 不在 PATH 上。
9. 模型推論直連公司 gateway：一個 Slack 問題得到回答、開頭沒有 model metadata warning，shell tool call 可用。
10. 圖片附件能進到模型；文件附件由 agent 從 OpenAB 提供的 R2 URL 下載轉文字後回答，轉不出來時明說。
11. 一般請求得到 PNG；明確要求 prototype 時才得到單一檔案的 HTML；都由受限 uploader 回原 thread。
12. 要求改一個 repo 的小地方時，agent 從 `/home/node/snapshots` clone、跑 `pnpm install`，並把 `.patch` 上傳到原 thread。
13. `company-image generate` 在 container 內產出 PNG 到 `/home/node/drafts/<UTC 日期>/`；非法 size、走出 drafts 的檔名、允許範圍外的來源圖片都被拒絕且不發出 gateway 請求。靜態部分由 `tests/image-runtime.py` 用假 gateway 驗，真的 gateway 回得回圖要在有 key 的環境實跑一次。
14. `slack-thread-artifact` 只接受 drafts 下的 regular `.png`、`.md`、`.html`、`.patch`；成功依序取得 upload URL、上傳、complete 到 `<sender_context>` 的 channel/thread 後刪檔，失敗不刪檔。兩個 Slack Web API 呼叫送 form body（`complete` 的 `files` 是 JSON 字串欄位），bytes 那一段用 Slack 回的 URL 與 artifact mime 且不帶 token。`tests/slack-thread-artifact.py` 逐項比對三個請求的 header 與 body；真的 Slack 收不收要在有 token 的環境實傳一次。
15. 未設定並啟用 STT 時 audio 被明確拒絕；設定獨立相容 endpoint 後才驗 `/audio/transcriptions` 真實轉錄。
16. 兩支 CLI 都走不出固定 root：父目錄被換成 symlink、檔名本身是 symlink、路徑用 `..` 逃逸，都被拒絕且不發出任何 Slack 或 gateway 請求。`company-image` 的目標檔名被事先種成指向外面的 symlink 時，寫到下一個可用檔名，不寫穿它。由 `tests/artifact-path-safety.py` 重現。
17. 上傳成功後暫存檔被刪；但暫存檔在讀取之後被換成別的檔案或 symlink 時，**不刪**那個東西，在 stderr 說明並仍 exit 0（檔案確實送出去了，回非零只會讓 agent 重傳）。
18. Deployment host 的 crontab 有 `# agent-company-artifact-cleanup` entry，每小時執行 `scripts/cleanup-artifacts.sh`，它在 container 內跑 `slack-list cleanup`；container 沒在跑時這一輪失敗會寫進 `artifact-cleanup.log` 並回非零。排程存在由 `tests/static.sh` render `render_crontab` 驗；cron 真的觸發過只能人工確認。
19. `tests/static.sh` 與 `scripts/deploy.sh` 都不聲稱驗過 Slack、公司 gateway、STT 或 cron 觸發；`deploy.sh` 結束時印出未驗清單，對應 [`runbook.md`](runbook.md#人工-release-gate) 的「人工 release gate」。

## 人工前置作業

- Slack app 重新安裝並取得 `xapp-...`、新 `xoxb-...`。
- 將 app 顯示名稱改為「派大星教授加博士先生」。
- 把 host 專用 GitHub SSH public key 加入一個能讀 `config/repos.conf` 內所有 private repos 的 GitHub 帳號。

實際操作命令見 [`runbook.md`](runbook.md)。
