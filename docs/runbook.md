# agent-company Runbook

Local 與 deployment host 共用同一份 Compose 及 runtime 設定。兩邊都要建立 root `.env`（snapshot root 與執行 docker 的使用者 uid/gid）與 `env/openab.env`（runtime secret）。Container 的 home 是 named volume `home`；本 repo 的 `runtime/` 只放 `scripts/deploy.sh` 產生的 `runtime/codex/config.toml`。

Deployment host 是一台 Linux host，Compose 以維護者自己的帳號執行，不需要 sudo。該帳號必須在 `docker` group 裡，並且 host 要有 Git、SSH client、Python 3（3.11 以上，`render_codex_config` 用 `tomllib`）、curl、jq 與 cron。SELinux enforcing 的 host 不需要額外設定，Compose 的 bind mount 已經帶 `:z`。

`compose.yaml` 已寫死 image digest 與 build args，`docker compose` 可直接用；下面的指令都寫成 `docker compose -f ./compose.yaml`，在 repo 根目錄執行。Compose project 是 `agent-company`，service 是 `agent`，container 是 `agent-company`。

## 1. 設定 Slack App

這只需做一次，local 與 deployment host 共用。

1. 開啟 **Socket Mode**。
2. 建立 App-Level Token，scope 選 `connections:write`，取得 `xapp-...`。
3. 開啟 **Event Subscriptions**，訂閱 bot events：`app_mention`、`message.groups`、`message.im`。
4. 到 **App Home** 開啟 **Messages Tab**，並允許使用者從 Messages Tab 傳送訊息。
5. 加入 bot token scopes：

```text
app_mentions:read
chat:write
files:read
files:write
groups:history
groups:read
im:history
lists:read
lists:write
reactions:write
users:read
```

6. **Reinstall to Workspace**，取得新的 `xoxb-...`。
7. 只把 app 邀進 `C0BPZRN6H3R` 與 `C0B9PSESQ2U`。

`files:read` 是讀取 Slack incoming attachment 的必要 scope；只新增 scope 不會更新既有 token，必須 **Reinstall to Workspace**。

Socket Mode 用 WebSocket 接收 events，所以不用填 Event Subscriptions 的 Request URL，也不用設定 Incoming Webhook。OpenAB 使用 `xoxb-...` 透過 Slack Web API 回覆訊息。

`users:read` scope 必須經人工確認已套用到 bot token；新增 scope 後要 **Reinstall to Workspace**。發布腳本用該 token 呼叫 [`users.list`](https://api.slack.com/methods/users.list)，逐頁列出目前這個 Slack workspace 的成員，略過已刪除、bot 與 app user，再對每位合格真人呼叫 [`views.publish`](https://api.slack.com/methods/views.publish)。Slack 的 `views.publish` 需要 `user_id`，沒有 workspace broadcast；這代表目前 workspace 的成員清單，不保證涵蓋其他 workspace。這個名單不代表 OpenAB 授權名單，也不會改變 `config/openab.toml` 的存取設定。

發布功能首頁：

```bash
./scripts/publish-slack-home.sh
```

文案放在 `config/slack-home.json`。修改後重跑同一指令即可；不需要訂閱 `app_home_opened`。若沒有合格成員或任一發布失敗，指令會以非零狀態結束，並回報成功／失敗數；失敗時會停止，不會把部分發布回報成成功。

此操作只發布 Home view，不改 OpenAB 使用者權限。因 OpenAB 目前需要 `allow_all_channels = true` 才可收 DM，不可把 app 邀進其他 channel，否則會擴大入口。

## 2. Local 首次啟動

同一個 Slack app 同時只能跑一個 OpenAB instance；local 測試前先停止正式 instance。

Local 不掛開發用的 checkout，而是自己建一份乾淨 snapshot，避免把各 repo 的 `.env` 掛進 container：

```bash
cd "$HOME/code/agent-company"

test -f .env || cp .env.example .env
sed -i "s/^HOST_UID=.*/HOST_UID=$(id -u)/; s/^HOST_GID=.*/HOST_GID=$(id -g)/" .env

test -f env/openab.env || cp env/openab.env.example env/openab.env
chmod 600 env/openab.env
$EDITOR env/openab.env

# Local 用自己的 GitHub key 即可，不必另外產一把。
GITHUB_SSH_KEY="$HOME/.ssh/<你的 GitHub key>" ./scripts/update-snapshots.sh

./tests/static.sh
./scripts/deploy.sh
docker compose -f ./compose.yaml logs --tail=100 agent
```

`deploy.sh` 會先跑 `preflight.sh`，再從 `config/codex/config.toml` 產生 `runtime/codex/config.toml`（填入 `COMPANY_GATEWAY_BASE_URL`），然後 build、啟動並驗 container 內的版本與讀寫權限。直接 `docker compose up` 會缺那個產生出來的檔，所以第一次一定要走 `deploy.sh`。第一次同步時 image 還沒 build，CodeGraph 索引那段會跳過；`deploy.sh` 跑完後再跑一次 `./scripts/update-snapshots.sh`（local 一樣帶 `GITHUB_SSH_KEY`）。

`env/openab.env` 每個欄位的說明寫在 [`env/openab.env.example`](../env/openab.env.example)。至少要填 Slack 的兩個 token、`SLACK_LIST_ID`、`SLACK_TEAM_ID`、`SLACK_WORKSPACE_URL`，以及公司 gateway 的 `COMPANY_GATEWAY_BASE_URL` 與 `COMPANY_GATEWAY_API_KEY`（Codex 用它跑模型，`company-image` 用它生圖，preflight 會擋），並保留：

```text
WORK_HELPER_ISSUE_MODE=manual
```

`COMPANY_GATEWAY_BASE_URL` 是 gateway 提供 Responses API 的前綴，必須是 https、結尾不帶斜線；Codex 與 `company-image` 都在後面接 `/responses`。填錯的症狀是推論回 HTTP 405，見 [ADR 0010](adr/0010-company-provider-uses-the-responses-api.md)。

這些 runtime secret 全部住在同一個 container，跟 agent 執行的 repo code 在一起，沒有 broker、proxy 也沒有 token 隔離層。理由與取捨見 [ADR 0012](adr/0012-follow-the-official-openab-codex-deployment.md)。

模型 ID 在 `config/codex/config.toml` 的 `model`，必須對得上公司 gateway 實際 expose 的名稱；`config/codex/catalog.json` 要有同一個 slug，不然每輪回覆開頭會出現 model metadata warning。

Root `.env` 只給 Compose 用（snapshot root 與 uid）；`env/openab.env` 才會傳進 container。`.env`、`env/openab.env` 與 `runtime/` 都不進 Git。

### Cloudflare R2 filestore（deployment host 手動前置）

`config/openab.toml` 的 `[filestore]` 是設定正本：bucket `work-agent-attachments`（外部資源，沿用舊名）、region `auto`、prefix `incoming/`、presigned URL TTL 3600 秒、單檔上限 **50 MiB**。設定檔不含任何 R2 credential；請在 deployment host 手動編輯未追蹤的 `env/openab.env`，加入：

```text
R2_ACCESS_KEY_ID=<Cloudflare R2 access key>
R2_SECRET_ACCESS_KEY=<Cloudflare R2 secret key>
```

完成後確認檔案權限仍是 `600` 或 `640`。Cloudflare R2 bucket `work-agent-attachments` 還必須手動建立 lifecycle rule，讓 `incoming/` 物件 **1 天後過期**；這個外部設定不在 repo 內，也不會被 static test 或 API 驗證。

`preflight.sh` 會拒絕缺少／placeholder 的 R2 secrets 與不對的 `[filestore].endpoint`，並在繼續前要求人工逐項確認 R2 lifecycle 與 Slack `files:read`；不會嘗試呼叫 Cloudflare 或 Slack API。沒有 interactive TTY 的 `preflight.sh`／`deploy.sh` 會安全中止。

文件附件（PDF、DOCX、XLSX、PPTX 等）由 OpenAB 把 R2 `incoming/` presigned URL 與原始 filename 放進 prompt；agent 自己下載、轉成文字再回答，需要的轉檔工具裝在 home。需要 OCR 的掃描檔轉不出來時，agent 會直說。presigned URL 會隨 prompt 送到公司 gateway。

### 在 Slack 確認

1. DM 詢問一個 repo 問題。
2. 在 `#你為什麼不問問神奇海螺ㄋ` @ bot 建立一筆實際要保留的待辦，確認指派給 sender 並保存來源。
3. 再次要求建立同名待辦，確認沒有新增第二列。
4. 在待辦列的 item 留言串 @ bot，確認它能找到正確的 `Rec...`。
5. 傳一張截圖問問題，確認圖片有被讀到；再傳一份 PDF 或 DOCX，確認 bot 讀了內容後回答。
6. 要一張圖，確認 bot 用 `company-image` 產出 PNG，再用 `slack-thread-artifact` 回同一個 thread；明確要求「可互動 prototype」時才上傳單一檔案 HTML。上傳成功後確認 drafts 原檔被刪；讓 Slack API 故意失敗一次，確認原檔保留給 cleanup。
   這一項也是 Slack upload body 編碼唯一的真實驗證：`files.getUploadURLExternal` 收到 JSON body 會回 `invalid_arguments`，檔案完全傳不出去。stderr 出現 `invalid_arguments` 就是編碼壞了，不是 token 或 scope 的問題（見 [`adr/0009`](adr/0009-thread-artifact-upload-and-optional-stt.md)）。
7. 要求改某個 repo 的一個小地方，確認 agent 從 `/home/node/snapshots` clone 到 home、跑 `pnpm install`，並把 `.patch` 上傳到同一個 thread。在自己的電腦用 `git am` 套得上才算過。

停止 local instance：

```bash
docker compose -f ./compose.yaml down
```

## 3. Deployment Host 首次部署

### GitHub 讀取權限

建立只能讀 snapshots 的 SSH key（檔名沿用 `work-agent-github`，`scripts/update-snapshots.sh` 預設讀這個路徑）：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/work-agent-github -C agent-company@deployment-host
chmod 600 ~/.ssh/work-agent-github
cat ~/.ssh/work-agent-github.pub
```

把 public key 加到能讀 `config/repos.conf` 內所有 private repos 的 GitHub account。Private key 只留在 host，不得放進 repo、Docker env 或 container。

```bash
ssh -T -i ~/.ssh/work-agent-github -o IdentitiesOnly=yes git@github.com
```

### 安裝

Checkout 放在哪由維護者決定；下面以 `$HOME/code/agent-company` 為例。

```bash
cd "$HOME/code/agent-company"

test -f .env || cp .env.example .env
sed -i "s/^HOST_UID=.*/HOST_UID=$(id -u)/; s/^HOST_GID=.*/HOST_GID=$(id -g)/" .env

test -f env/openab.env || cp env/openab.env.example env/openab.env
chmod 600 env/openab.env
$EDITOR env/openab.env

./scripts/install-sync-cron.sh
./scripts/deploy.sh
./scripts/update-snapshots.sh   # image 已 build，這次才會建 CodeGraph 索引
docker compose -f ./compose.yaml logs --tail=100 agent
```

`install-sync-cron.sh` 會建立 snapshot root 與 `$XDG_STATE_HOME/agent-company`、寫入**兩個**每小時的 crontab entry，並跑第一次同步：

| Marker | 時間 | 做什麼 |
|---|---|---|
| `# agent-company-snapshots` | 每小時 :00 | `scripts/update-snapshots.sh`：同步 snapshot、重建 CodeGraph 索引 |
| `# agent-company-artifact-cleanup` | 每小時 :17 | `scripts/cleanup-artifacts.sh`：在 container 內跑 `slack-list cleanup` |

兩行都由 `scripts/lib.sh` 的 `render_crontab` 產生，`tests/static.sh` 會 render 同一個函式來確認 cleanup 真的有排程。上傳失敗的 artifact 是刻意留在 drafts 的，第二行是唯一會把它們收掉的東西；只裝第一行的 host 會讓 drafts 無限長大。crontab 裡寫的是 script 的絕對路徑，checkout 搬家後要重跑 `install-sync-cron.sh`。

保留期限是 24 小時，但檢查每小時才一次，所以實際壽命是 **24 到 25 小時**，不是剛好 24。

`deploy.sh` 會先執行 preflight，再產生 Codex 設定、build 及啟動 container，最後印出它**沒有**驗到的項目（見下面「人工 release gate」）。

### 驗證安全邊界

`deploy.sh` 已經驗過下面大部分項目；改過 mount、Dockerfile 或 `config/openab.toml` 之後再手動跑一次：

```bash
docker compose -f ./compose.yaml ps
crontab -l | grep agent-company-
docker compose -f ./compose.yaml exec agent sh -lc \
  'python3 --version >/dev/null &&
   git --version >/dev/null &&
   /home/node/snapshots/agent-config/skills/slack-list/scripts/slack-list --help >/dev/null &&
   test -w /home/node && test -w /home/node/.codex &&
   test ! -w /home/node/snapshots &&
   test ! -w /home/node/snapshots/teamsync-frontend &&
   test -w /home/node/snapshots/.index &&
   test -e /home/node/.agents/skills/slack-list/SKILL.md &&
   grep -q "^\[mcp_servers\." /home/node/.codex/config.toml &&
   test ! -w /etc/codex/config.toml && test ! -w /etc/codex/AGENTS.bot.md &&
   curl -fsS http://127.0.0.1:9222/json/version >/dev/null &&
   test ! -w /etc/openab/config.toml && test ! -w /etc/codex/catalog.json &&
   test ! -e /home/node/.ssh &&
   test ! -e /home/node/.config/gh &&
   ! gh auth status >/dev/null 2>&1 &&
   command -v company-image >/dev/null &&
   command -v slack-thread-artifact >/dev/null &&
   test ! -w /usr/local/bin/company-image &&
   test ! -w /usr/local/bin/slack-thread-artifact &&
   codegraph explore boot -p /home/node/snapshots/agent-config >/dev/null'
```

`test -w` 這幾項是 SELinux label 與 uid 都正確才會過的，Compose render 成功不代表通過。`gh` binary 是 OpenAB 官方 image 自帶的；這裡確認的是 container 沒有任何 GitHub 憑證。home 可寫，所以 `.ssh` 不存在只代表目前沒有人放 key 進去，不是 mount 擋住的。

生圖那條路徑要真的打一次公司 gateway 才算驗過（會產生一次計費呼叫）：

```bash
docker compose -f ./compose.yaml exec agent \
  company-image generate --prompt 'a plain grey square' --name smoke
docker compose -f ./compose.yaml exec agent sh -lc \
  'head -c 8 "$(ls -t /home/node/drafts/*/smoke*.png | head -1)" | od -c | head -1'
```

第一行會印出 drafts 內的 PNG 路徑，第二行確認開頭是 PNG magic。驗完把那個檔刪掉。`tests/image-runtime.py` 只驗請求 shape、兩種回應形狀（JSON body 與 `text/event-stream`）與拒絕不合法輸入，**不會**證明公司 gateway 接受這個請求；gateway 沒實際回過圖之前不要說生圖已經可用。

### 人工 release gate

`tests/static.sh` 是靜態檢查，`scripts/deploy.sh` 只驗到「container 起得來、Codex 版本對、provider 設定已產生且唯讀、home 可寫而 snapshot 與 skills 不可寫」；preflight 另外只做 R2 secret／endpoint 檢查和 R2 lifecycle、Slack `files:read` 的人工 checklist。**不會呼叫 Slack、Cloudflare、公司 gateway 或 STT，也不會證明 crontab 真的觸發過。** 沒有 Docker 的機器上 `tests/static.sh` 連 Compose render 都是本機解析（它會印 LIMITED VERIFICATION）。

所以下面這幾項是人工 gate，每次 release 都要重跑一遍，不能因為 `deploy.sh` 退出 0 就當它們過了。編號與 `deploy.sh` 結束時印的清單相同：

| # | 要驗什麼 | 怎麼驗 | 為什麼自動化驗不到 |
|---|---|---|---|
| 1 | Slack 往返與授權邊界 | 授權帳號 DM 有回應；未授權（不在 app 事件範圍內）的訊息沒有 | 需要真的 Slack workspace 與兩個身分 |
| 2 | Artifact 回原 thread | 上面「在 Slack 確認」第 6 項：檔案出現在原 thread（不是 `invalid_arguments`）、drafts 原檔被刪；刻意失敗一次確認留檔 | 需要真的 `SLACK_BOT_TOKEN` 與真的 thread；離線測試只比對 header 與 body 字串 |
| 3 | 生圖 | 上面那兩行 `company-image generate`（會產生一次計費呼叫） | 離線測試只驗 request shape |
| 4 | Artifact cleanup 排程 | `./scripts/cleanup-artifacts.sh` 手動跑一次，再確認 crontab entry 存在、log 沒有 `FAILED` | 靜態測試只證明排程被寫進去，不證明 cron 觸發過 |
| 5 | STT（`[stt].enabled = true` 時才要） | 用真實音檔打一次 `/audio/transcriptions` | 靜態檢查不發請求 |
| 6 | 模型推論直連公司 gateway | 一個 Slack 問題得到回答，開頭沒有 model metadata warning；一次 shell tool call 可用；同一 thread 第二次 @ 恢復原 session（`session/load`），不是重開 | 離線沒有任何東西能證明 gateway 收這條路由 |
| 7 | Coding 流程 | 上面「在 Slack 確認」第 7 項：clone、`pnpm install`、`.patch` 上傳到 thread | 需要真的模型、Slack 與網路 |

第 4 項的指令：

```bash
crontab -l | grep agent-company-artifact-cleanup
./scripts/cleanup-artifacts.sh
tail -n 20 "${XDG_STATE_HOME:-$HOME/.local/state}/agent-company/artifact-cleanup.log"
```

第 6 項失敗時看 `docker compose -f ./compose.yaml logs --tail=200 agent`。HTTP 405 代表請求沒打到 Responses endpoint，先查 `COMPANY_GATEWAY_BASE_URL` 的前綴（見 [ADR 0010](adr/0010-company-provider-uses-the-responses-api.md)）；401／403 是 key 的問題。第二次 @ 變成新 session 時，先確認 `config/openab.toml` 的 `CODEX_HOME` 仍指向 home volume 裡的 `/home/node/.codex`。改動 `config/codex/`、`COMPANY_GATEWAY_BASE_URL` 或 Codex 版本之後都要重跑這一項。

`deploy.sh` 結束時會把這份清單再印一次，避免只看終端機輸出的人以為已經驗完。

### 啟用 audio 轉錄（可選）

不要把 company gateway 當成 STT provider。先用一個已確認支援 OpenAI-compatible `/audio/transcriptions` 的 endpoint 填 `STT_BASE_URL`、`STT_API_KEY`，再把 `config/openab.toml` 的 `[stt].enabled` 改成 `true`，重新跑 `./tests/static.sh && ./scripts/deploy.sh`。未做完這些步驟時 audio 必須被 bot 明確拒絕。首次驗證要以真實音檔打一次 STT endpoint；靜態檢查不會發請求。

## 4. 日常操作

不要直接在 deployment host 修改 repo 檔案。所有設定與文件都先在 local 修改、commit 及 push；host 只執行 `git pull --ff-only` 與部署命令。

更新部署：

```bash
cd "$HOME/code/agent-company"
git pull --ff-only
./tests/static.sh
./scripts/deploy.sh
```

查看 agent：

```bash
docker compose -f ./compose.yaml ps
docker compose -f ./compose.yaml logs -f --tail=200 agent
```

查看或立即更新 snapshots：

```bash
crontab -l | grep agent-company-snapshots
tail -n 50 "${XDG_STATE_HOME:-$HOME/.local/state}/agent-company/snapshots.log"
./scripts/update-snapshots.sh
```

查看或立即執行 artifact cleanup：

```bash
crontab -l | grep agent-company-artifact-cleanup
tail -n 50 "${XDG_STATE_HOME:-$HOME/.local/state}/agent-company/artifact-cleanup.log"
./scripts/cleanup-artifacts.sh
```

`cleanup-artifacts.sh` 自己不刪檔，它在 container 內執行 `/home/node/snapshots/agent-config/skills/slack-list/scripts/slack-list cleanup`——刪除規則的正本在 agent-config。drafts 在 home volume 裡，host 不要繞過這支去刪。log 裡出現 `FAILED` 就是那一輪沒清到（多半是 container 沒在跑），artifact 會留到下一輪；持續 FAILED 要當成待處理，不是雜訊。

`update-snapshots.sh` 同步完會用 runtime image 重建 CodeGraph 索引，所以 host 不需要安裝 Node。索引放在 `$SNAPSHOT_ROOT/.index/<repo>`，每個 snapshot 內的 `.codegraph` 是指過去的相對 symlink。image 還沒 build 時那一輪的索引會被跳過並印出訊息，`deploy.sh` 之後再跑一次同步即可。

查看 home 用了多少空間（home 沒有自動清理，clone 與套件快取會累積）：

```bash
docker compose -f ./compose.yaml exec agent sh -lc 'du -sh /home/node/* /home/node/.[!.]* 2>/dev/null | sort -h | tail -20'
```

要清就在 container 內刪 agent clone 出來的目錄或套件快取；不要動 `/home/node/.codex`（Codex session）與 `/home/node/.openab`（OpenAB state）。

停止 agent：

```bash
docker compose -f ./compose.yaml down
```

不要執行 `docker compose -f ./compose.yaml down -v`，它會刪掉 `home` volume：Codex session、OpenAB state、還沒上傳的 drafts 與 agent 裝好的工具都會一起消失。

### 從 OpenCode 版本切換過來（一次性）

這一版把 repo、Compose project、service 與 container 都改了名，snapshot 在 container 內的路徑也從 `/home/node/code` 改成 `/home/node/snapshots`。舊 container 屬於另一個 Compose project，新的 `deploy.sh` 不會幫你停；同一個 Slack app 不能同時跑兩個 instance，所以先停舊的：

```bash
docker stop work-agent && docker rm work-agent
```

接著在 checkout 內：

```bash
git pull --ff-only
set -a; . ./.env; set +a          # 取得 SNAPSHOT_ROOT
# 舊索引記著 /home/node/code 底下的絕對路徑；清空讓它在新路徑重建。
rm -rf "${SNAPSHOT_ROOT:?}"/.index/*
./scripts/install-sync-cron.sh   # 換掉舊的 # work-agent-* 兩行，寫入 # agent-company-* 兩行
./scripts/deploy.sh
./scripts/update-snapshots.sh    # 新 image 已 build，這次重建索引
```

`SNAPSHOT_ROOT` 沿用 root `.env` 裡原本的值即可，不必搬目錄。`install-sync-cron.sh` 會一併拿掉舊 marker `# work-agent-snapshots` 與 `# work-agent-artifact-cleanup` 的兩行；跑完用 `crontab -l` 確認只剩 `agent-company-` 兩行。

確認新部署通過「人工 release gate」之後，再刪舊 runtime 留下的東西：

```bash
docker volume rm work-agent_opencode-config work-agent_opencode-state \
  work-agent_opencode-data work-agent_opencode-cache work-agent_claude-credentials
docker network rm work-agent_default 2>/dev/null || true
rm -rf runtime/openab runtime/drafts
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/work-agent"
```

`runtime/openab` 與 `runtime/drafts` 是舊版的 OpenAB state 與 artifact 暫存 bind mount；新版這兩樣都在 home volume 裡。舊 drafts 裡若還有沒上傳的檔，先確認不需要再刪。

### 升級版本

版本正本只有 `config/versions.env` 一份，每一項的升級規則寫在該檔註解。`compose.yaml` 的 build args 要同步改成相同的值。

升級 OpenAB 時：

1. 取得新的 OpenAB `-codex` multi-arch digest，確認新版 Slack config 沒有不相容的改動。
2. 更新 `OPENAB_VERSION`、`OPENAB_IMAGE` 與 `compose.yaml` 的 `OPENAB_IMAGE` build arg。
3. `./tests/static.sh && ./scripts/deploy.sh`，再跑人工 release gate。

升級 Codex 時 `CODEX_VERSION` 與 `CODEX_ACP_VERSION` 一起升，並且：

1. 把 `config/openab.toml` 裡 `CODEX_PATH` 的版本號改成新版。
2. 從新版 codex 的 `codex-rs/models-manager/models.json` 重產 `config/codex/catalog.json`（做法見 `config/codex/config.toml` 的註解）。
3. `./tests/static.sh && ./scripts/deploy.sh`，再跑人工 release gate 第 6 項。build 時會驗 exact version，對不上會失敗，這是刻意的。

### 更新 Skill、MCP 或個人規則

bot 的 skills、MCP 與個人規則跟維護者機器同一份，正本是 agent-config。在任一台機器照平常 `skillshare install`／`mcp add`、push 到 agent-config 後，host 每小時同步 snapshot，結束時在 bot 裡跑 `agent-sync`（`skillshare sync -g`、`skillshare sync mcp -g`，再重組 `~/.codex/AGENTS.md`）。要立即生效就在 host 上執行 `./scripts/update-snapshots.sh`；已開的 Codex session 要等下一個 session 才讀到。

MCP 的 key 用 `fromEnv`：在 `env/openab.env` 填同名變數，並加進 `config/openab.toml` 的 `inherit_env`（`CONTEXT7_API_KEY` 已加）。`agent-sync` 失敗時 `snapshots.log` 會有 `FAILED: agent-sync`；啟動時失敗會讓 OpenAB 不啟動，看 `docker compose logs agent`。

`slack-list` 設定只讀環境變數：container 從 `env/openab.env` 拿到，不需要 `~/.config/slack-list/.env`。

從 work-helper 切過來的 host，snapshot root 裡還留著舊的 `work-helper` 目錄，`preflight.sh` 會因為它不在 `config/repos.conf` 而失敗。確認新的 `agent-config` snapshot 同步成功後，刪掉 `$SNAPSHOT_ROOT/work-helper` 與 `$SNAPSHOT_ROOT/.index/work-helper`。

已啟動的 session 可能已把舊 skill 內容讀進 context。同步後使用新的 Slack thread 或等待 session 回收再驗證，不用以舊 session 判斷同步失敗。

### 新增 Repo

新增 repo 會擴大 agent 可讀、可 clone 的範圍，必須當成能力與權限變更 review。整個 snapshot root 是單一 mount，所以只改一個檔案：

| 正本 | 要改什麼 |
|---|---|
| `config/repos.conf` | 新增一行 `name\|SSH remote\|基準branch` |

同時更新 `config/slack-home.json` 的「可查閱的專案」，`tests/static.sh` 會擋漏掉的 repo 名稱。

Snapshot key 對應的 GitHub 帳號必須先取得新 repo 的 read 權限。Push deployment repo 後，在 host 執行：

```bash
git pull --ff-only
./scripts/update-snapshots.sh
./scripts/preflight.sh
./scripts/deploy.sh
./scripts/publish-slack-home.sh
```

`preflight.sh` 會擋下 snapshot root 裡出現不在 `config/repos.conf` 的目錄，避免有東西被掛進 container 卻沒有經過 review。

最後依「驗證安全邊界」確認所有 repo snapshot 不可寫，並從新的 Slack thread 詢問新 repo 內容。
