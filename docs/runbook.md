# Work Agent Runbook

Local與deployment host共用同一份 Compose及runtime設定。State與 artifact 暫存檔都放在本 repo的 `runtime/`；
兩邊都要建立 root `.env`，指定 snapshot root與執行 docker的使用者 uid/gid。

Deployment host是一台 Linux host，Compose以維護者自己的帳號執行，不需要sudo。該帳號必須在`docker` group裡，
並且host要有Git、SSH client、Python 3、curl、jq與cron。SELinux enforcing的host不需要額外設定，Compose的
bind mount已經帶`:z`。

**所有 compose 指令都走 `docker compose -f ./compose.yaml`。** 它會先載入 root `.env` 與
`config/versions.env`，再把參數原樣交給 `docker compose`。直接打 `docker compose` 會因為
`OPENAB_IMAGE` 未設定而中止 —— 這是刻意的，版本只有 `config/versions.env` 一份正本。

Compose service 仍然叫 `backlog-agent`，container 叫 `work-agent`。這是識別字，不是產品定位；
改名會讓現有部署的 volume 與 crontab 對不上，所以沒有跟著文件一起改。

## 1. 設定 Slack App

這只需做一次，local與deployment host共用。

1. 開啟 **Socket Mode**。
2. 建立 App-Level Token，scope選 `connections:write`，取得 `xapp-...`。
3. 開啟 **Event Subscriptions**，訂閱 bot events：`app_mention`、`message.groups`、`message.im`。
4. 到 **App Home** 開啟 **Messages Tab**，並允許使用者從 Messages Tab傳送訊息。
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
7. 只把 app邀進 `C0BPZRN6H3R` 與 `C0B9PSESQ2U`。

`files:read` 是讀取 Slack incoming attachment 的必要 scope；只新增 scope 不會更新既有
token，必須 **Reinstall to Workspace**。

Socket Mode用 WebSocket接收 events，所以不用填 Event Subscriptions的 Request URL，也不用設定
Incoming Webhook。OpenAB使用 `xoxb-...` 透過 Slack Web API回覆訊息。

`users:read` scope 必須經人工確認已套用到 bot token；新增 scope 後要 **Reinstall to Workspace**。
發布腳本用該 token 呼叫 [`users.list`](https://api.slack.com/methods/users.list)，逐頁列出
目前這個 Slack workspace 的成員，略過已刪除、bot 與 app user，再對每位合格真人呼叫
[`views.publish`](https://api.slack.com/methods/views.publish)。Slack 的 `views.publish`
需要 `user_id`，沒有 workspace broadcast；這代表目前 workspace 的成員清單，不保證涵蓋其他
workspace。這個名單不代表 OpenAB 授權名單，也不會改變 `config/openab.toml` 的存取設定。

發布功能首頁：

```bash
./scripts/publish-slack-home.sh
```

文案放在 `config/slack-home.json`。修改後重跑同一指令即可；不需要訂閱 `app_home_opened`。
若沒有合格成員或任一發布失敗，指令會以非零狀態結束，並回報成功／失敗數；失敗時會停止，
不會把部分發布回報成成功。

此操作只發布 Home view，不改 OpenAB 使用者權限。因 OpenAB目前需要 `allow_all_channels = true`
才可收 DM，不可把 app邀進其他 channel，否則會擴大入口。

## 2. Local 首次啟動

同一個 Slack app同時只能跑一個 OpenAB instance；local測試前先停止正式 instance。

Local不掛開發用的 checkout，而是自己建一份乾淨 snapshot，避免把各 repo的 `.env` 掛進 container：

```bash
cd "$HOME/code/work-agent-deploy"

test -f .env || cp .env.example .env
sed -i "s/^HOST_UID=.*/HOST_UID=$(id -u)/; s/^HOST_GID=.*/HOST_GID=$(id -g)/" .env

test -f env/openab.env || cp env/openab.env.example env/openab.env
chmod 600 env/openab.env
$EDITOR env/openab.env

# Local用自己的 GitHub key即可，不必另外產一把。
GITHUB_SSH_KEY="$HOME/.ssh/<你的 GitHub key>" ./scripts/update-snapshots.sh
mkdir -p runtime/openab runtime/drafts

./tests/static.sh
./tests/parse-document.py  # parser unit；不宣稱實際測到 multiprocessing worker terminate timeout
./scripts/preflight.sh
docker compose -f ./compose.yaml build --pull
docker compose -f ./compose.yaml up -d
docker compose -f ./compose.yaml ps
docker compose -f ./compose.yaml logs --tail=100 backlog-agent
```

預設 runtime 是 OpenCode，模型走公司 gateway 的 API key，**不需要互動式登入**。OMO 可委派 Claude
Code ACP specialist；它直接使用 image 內固定版 `claude-agent-acp`，不透過 runtime `npx` 下載，
並與 rollback 共用 `claude-credentials` named volume。

`env/openab.env` 至少要填入 `SLACK_BOT_TOKEN`、`SLACK_APP_TOKEN`、`SLACK_LIST_ID`、
`SLACK_TEAM_ID`（後兩個 `slack-list` 與 `publish-slack-home.sh` 都會用，缺了會直接中止），
以及公司 gateway 的 `COMPANY_GATEWAY_BASE_URL` 與 `COMPANY_GATEWAY_API_KEY`（兩個 runtime 都
必填：OpenCode 用它跑模型，`company-image` 用它生圖，preflight 會擋），並保留：

```text
WORK_HELPER_ISSUE_MODE=manual
```

這些 runtime secret 全部住在同一個 container，沒有 broker 也沒有 token 隔離層。理由與取捨見
[`adr/0007-single-container-opencode-runtime.md`](adr/0007-single-container-opencode-runtime.md)。

`config/opencode/opencode.json` 與 `config/opencode/oh-my-opencode-slim.json` 裡的模型 ID
必須對得上公司 gateway 實際 expose 的名稱。新模型的本機 context、output、reasoning、tool call
與 image attachment metadata 是候選設定，不代表 gateway 已提供或支援；第一次接上 gateway 時先確認：

```bash
docker compose -f ./compose.yaml exec backlog-agent opencode models
```

對不上就依 gateway 實際名稱更新這兩份設定，保持主/小模型及 preset 使用的 ID 都在
`provider.company.models` 中（`tests/static.sh` 會比對 preset 用到的模型）。

Root `.env` 只給 Compose用（snapshot root與 uid）；`env/openab.env` 才會傳進 container。
`.env`、`env/openab.env` 與 `runtime/` 都不進 Git。Claude login存在 named volume
`claude-credentials`，重建 container後仍保留。

### Cloudflare R2 filestore（deployment host 手動前置）

唯一 OpenAB runtime config 的 `[filestore]` 必須相同：bucket 是
`work-agent-attachments`，endpoint 是
`https://99de68928da234ebcf0c9370443ad7ee.r2.cloudflarestorage.com`，region 是 `auto`，
prefix 是 `incoming/`，presigned URL TTL 是 3600 秒，單檔上限是 **50 MiB**。設定檔不含
任何 R2 credential；請在 deployment host 手動編輯未追蹤的 `env/openab.env`，加入：

```text
R2_ACCESS_KEY_ID=<Cloudflare R2 access key>
R2_SECRET_ACCESS_KEY=<Cloudflare R2 secret key>
```

完成後確認檔案權限仍是 `600` 或 `640`。Cloudflare R2 bucket
`work-agent-attachments` 還必須手動建立 lifecycle rule，讓 `incoming/` 物件 **1 天後
過期**；這個外部設定不在 repo 內，也不會被 static test 或 API 驗證。

Compose 會把 `PARSE_DOCUMENT_ALLOWED_HOST=99de68928da234ebcf0c9370443ad7ee.r2.cloudflarestorage.com`
提供給 runtime，這只作 URL host gate。`preflight.sh` 會拒絕缺少／placeholder 的 R2 secrets、錯誤
host，並在繼續前要求人工逐項確認 R2 lifecycle 與 Slack `files:read`；不會嘗試呼叫 Cloudflare
或 Slack API。沒有 interactive TTY 的 `preflight.sh`／`deploy.sh` 會安全中止。

#### 文件附件操作

PDF、DOCX、XLSX、PPTX 是正式支援的文件附件。OpenAB 將 R2 `incoming/` presigned URL 與原始
filename 注入 ACP prompt；URL 有效 1 小時、大小上限 50 MiB。agent 收到後自動以
`parse-document <url> <filename>` 下載並轉成 Markdown，再以 Markdown 回答。這個 URL 是
OpenAB 提供的唯一輸入，agent 不得使用使用者文字中的 URL、改寫 URL 或自行下載；URL 會隨 ACP
prompt 傳給 company gateway，是已明確同意的 trust boundary。ZIP 只用同一個指令安全列出
metadata，不解壓、不讀 member 內容；video 仍直接回覆不支援。

#### 文件附件支援矩陣

| 輸入類型 | 狀態 | 處理方式與限制 |
|---|---|---|
| Slack 文字訊息 | 支援 | OpenAB 直接注入 ACP prompt，由 company gateway 的模型處理。 |
| 圖片 | 支援 | OpenAB 原生 media 路徑交給支援視覺輸入的模型處理；不經 `parse-document` 或 Docling。 |
| PDF | 支援文字型 PDF | OpenAB 傳 R2 presigned URL；`parse-document` 下載後用 Docling 轉 Markdown，保留 layout/table 結構；最多 200 頁。掃描 PDF 不支援 OCR。 |
| DOCX、XLSX、PPTX | 支援 | OpenAB 傳 R2 presigned URL；`parse-document` 下載後用 Docling Office backend 轉 Markdown。 |
| ZIP | 僅列檔 | OpenAB 傳 R2 presigned URL；`parse-document` 最多列出 1,000 個安全檔名與 metadata，不解壓、不讀內容。 |
| CSV、`.doc`、`.xls`、`.ppt` | 不支援 | `parse-document` 只接受 PDF 與 OOXML 格式。 |
| audio | 不支援 | STT 未啟用；不轉錄。 |
| video | 不支援 | 不下載、轉檔或解析。 |

所有文件單檔上限 50 MiB。下載與 Docling 解析各有 120 秒上限；轉出的 Markdown 最多 10 MiB。
PDF 使用 image build 時預取的 layout/table artifacts，runtime 不下載模型。

在 Slack確認：

1. DM詢問一個 repo問題。
2. 在 `#你為什麼不問問神奇海螺ㄋ` @ bot建立一筆實際要保留的待辦，確認指派給sender並保存來源。
3. 再次要求建立同名待辦，確認沒有新增第二列。
4. 在待辦列的 item留言串 @ bot，確認它能找到正確的 `Rec...`。
5. 傳一張截圖問問題，確認圖片有被讀到；再傳 PDF、DOCX、XLSX 或 PPTX，確認 bot 由附件
   轉 Markdown 後回答；傳 ZIP 確認只列檔，傳 video 確認明確回覆不支援。
6. 要一張圖，確認 bot 用 `company-image` 產出 PNG，再用 `slack-thread-artifact` 回同一個 thread；明確要求
   「可互動 prototype」時才上傳單一檔案 HTML。上傳成功後確認 drafts 原檔被刪；讓 Slack API 故意失敗一次，
   確認原檔保留給 cleanup。
   這一項也是 Slack upload body 編碼唯一的真實驗證：`files.getUploadURLExternal` 收到 JSON body 會回
    `invalid_arguments`，檔案完全傳不出去。stderr 出現 `invalid_arguments` 就是編碼壞了，不是 token 或
    scope 的問題（見 [`adr/0009`](adr/0009-thread-artifact-upload-and-optional-stt.md)）。

### Docling production runtime baseline

`config/versions.env` 的 `DOCLING_VERSION=2.121.0` 是唯一版本正本；Compose 將它當 build arg
傳給 Dockerfile。Docker build 會 exact-install 這個版本，執行
`docling-tools models download layout tableformer --output-dir /opt/docling-models`，只預取
layout/table artifacts，再把目錄設成唯讀並在 build time 以 `node` 驗證可讀。Runtime 固定使用
`DOCLING_ARTIFACTS_PATH=/opt/docling-models`；`parse-document` 不會在 runtime 下載模型，PDF
pipeline 會明確關閉 OCR 並開啟 table structure。這個 image 不額外安裝 OCR、VLM、video、ASR
或 LibreOffice。

目前不支援掃描 PDF OCR；需要 OCR 的文件必須明確回報無法讀取，不得猜測內容。未來要支援掃描
PDF，必須另加並審核 OCR engine，不可把 RapidOCR 或其他 OCR model 偷渡進目前的 prefetch。

在 deployment host 完成 build 後，維護者必須逐字執行：

```bash
docker compose -f ./compose.yaml build --pull
docker compose -f ./compose.yaml run --rm --user node --entrypoint parse-document backlog-agent --help
docker compose -f ./compose.yaml run --rm --user node --entrypoint sh backlog-agent -lc \
  'test "$DOCLING_ARTIFACTS_PATH" = /opt/docling-models &&
   test -d "$DOCLING_ARTIFACTS_PATH" &&
   test -r "$DOCLING_ARTIFACTS_PATH" && test -x "$DOCLING_ARTIFACTS_PATH" &&
   python3 -c '\''from pathlib import Path; import os; p=Path("/opt/docling-models"); assert any(p.iterdir()); assert all(os.access(x, os.R_OK | (os.X_OK if x.is_dir() else 0)) for x in p.rglob("*"))'\'''
```

以上只證明 exact pin、selective prefetch directory、environment 與 `node` permissions；沒有
宣稱 offline PDF conversion。要驗證一般 text PDF、layout/table 與 Office conversion，仍須用
release gate 的真實附件測試；掃描 PDF OCR 不在目前支援範圍。

停止 local instance：

```bash
docker compose -f ./compose.yaml down
```

## 3. Deployment Host 首次部署

### GitHub 讀取權限

建立只能讀 snapshots的 SSH key：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/work-agent-github -C work-agent@deployment-host
chmod 600 ~/.ssh/work-agent-github
cat ~/.ssh/work-agent-github.pub
```

把 public key加到能讀 `config/repos.conf` 內所有 private repos的 GitHub account。
Private key只留在 host，不得放進 repo、Docker env或 container。

```bash
ssh -T -i ~/.ssh/work-agent-github -o IdentitiesOnly=yes git@github.com
```

### 安裝

```bash
cd "$HOME/code/work-agent-deploy"

test -f .env || cp .env.example .env
sed -i "s/^HOST_UID=.*/HOST_UID=$(id -u)/; s/^HOST_GID=.*/HOST_GID=$(id -g)/" .env

test -f env/openab.env || cp env/openab.env.example env/openab.env
chmod 600 env/openab.env
$EDITOR env/openab.env

./scripts/install-sync-cron.sh
./scripts/deploy.sh
docker compose -f ./compose.yaml logs --tail=100 backlog-agent
```

`install-sync-cron.sh` 會建立 snapshot root與 repo內的 `runtime/`、寫入**兩個**每小時的 crontab entry，並跑第一次同步：

| Marker | 時間 | 做什麼 |
|---|---|---|
| `# work-agent-snapshots` | 每小時 :00 | `scripts/update-snapshots.sh`：同步 snapshot、重建 CodeGraph 索引 |
| `# work-agent-artifact-cleanup` | 每小時 :17 | `scripts/cleanup-artifacts.sh`：在 container 內跑 `slack-list cleanup` |

兩行都由 `scripts/lib.sh` 的 `render_crontab` 產生，`tests/static.sh` 會 render 同一個函式來確認
cleanup 真的有排程。上傳失敗的 artifact 是刻意留在 drafts 的，第二行是唯一會把它們收掉的東西；
只裝第一行的 host 會讓 drafts 無限長大。

保留期限是 24 小時，但檢查每小時才一次，所以實際壽命是 **24 到 25 小時**，不是剛好 24。

`deploy.sh` 會先執行 preflight，再 build及啟動 container，最後印出它**沒有**驗到的項目（見下面
「人工 release gate」）。

### 驗證安全邊界

```bash
docker compose -f ./compose.yaml ps
crontab -l | grep work-agent-snapshots
docker compose -f ./compose.yaml exec backlog-agent sh -lc \
  'python3 --version >/dev/null &&
   git --version >/dev/null &&
   /home/node/code/agent-config/skills/slack-list/scripts/slack-list --help >/dev/null &&
   test -w /home/node/.openab &&
   test -w /home/node/drafts &&
   test ! -w /home/node/code/teamsync-frontend &&
   test ! -w /home/node/code/teamsync-backend &&
   test -d /home/node/.claude/skills/slack-list &&
   test ! -e /home/node/.ssh &&
   test ! -e /home/node/.config/gh &&
   ! gh auth status >/dev/null 2>&1 &&
    command -v company-image >/dev/null &&
    command -v slack-thread-artifact >/dev/null &&
   test ! -w /usr/local/bin/company-image &&
   codegraph explore boot -p /home/node/code/agent-config >/dev/null'
```

生圖那條路徑要真的打一次公司 gateway 才算驗過（會產生一次計費呼叫）：

```bash
docker compose -f ./compose.yaml exec backlog-agent \
  company-image generate --prompt 'a plain grey square' --name smoke
docker compose -f ./compose.yaml exec backlog-agent sh -lc \
  'head -c 8 "$(ls -t /home/node/drafts/*/smoke*.png | head -1)" | od -c | head -1'
```

第一行會印出 drafts 內的 PNG 路徑，第二行確認開頭是 PNG magic。驗完把那個檔刪掉。
`tests/image-runtime.py` 只驗請求 shape、兩種回應形狀（JSON body 與 `text/event-stream`）與拒絕
不合法輸入，**不會**證明公司 gateway 接受這個請求，也不會告訴你它這次回哪一種；gateway 沒實際
回過圖之前不要說生圖已經可用。

OpenCode runtime 另外驗這幾項（`deploy.sh` 會驗版本與主要 writable mounts；下面另外驗 OMO file URI 與 XDG routing）：

OMO 必須由 image 內的 global package 載入，不可讓 OpenCode 看到 npm registry
package spec；`file://` URI 才能在 `network=none` 時載入 OMO/ACP wrapper。OpenCode
1.18.13 的 XDG state 也固定導向既有可寫的 `opencode-state` volume：

```bash
docker compose -f ./compose.yaml exec backlog-agent sh -lc \
  'opencode --version &&
   npm ls -g --depth=0 oh-my-opencode-slim &&
   test -f /usr/local/lib/node_modules/oh-my-opencode-slim/package.json &&
   test -n "$(node -p '\''require("/usr/local/lib/node_modules/oh-my-opencode-slim/package.json").main'\'')" &&
   test -f /usr/local/lib/node_modules/oh-my-opencode-slim/"$(node -p '\''require("/usr/local/lib/node_modules/oh-my-opencode-slim/package.json").main'\'')" &&
   grep -Fq "file:///usr/local/lib/node_modules/oh-my-opencode-slim" /home/node/.config/opencode/opencode.json &&
   test "$XDG_STATE_HOME" = /home/node/.opencode &&
   test ! -w /home/node/.config/opencode/opencode.json &&
   test ! -w /home/node/.config/opencode/oh-my-opencode-slim.json &&
   test -w /home/node/.config/opencode &&
   touch /home/node/.config/opencode/.probe && rm /home/node/.config/opencode/.probe &&
   test -w /home/node/.opencode &&
   touch /home/node/.opencode/.probe && rm /home/node/.opencode/.probe &&
   test ! -w /home/node &&
   test -w /home/node/.cache &&
   test -w /home/node/.local/share/opencode &&
   grep -q "^command = \"opencode\"" /etc/openab/config.toml'
```

`test -w` 這幾項是SELinux label與uid都正確才會過的，Compose render成功不代表通過。

OpenCode 有**兩個**必須可寫的目錄，各撞死過 `opencode acp` 一次。兩次的症狀一樣：OpenAB 端只
看得到 ACP 連線關掉，真正的原因要看 container log。

| 目錄 | volume | container log |
|---|---|---|
| `/home/node/.config/opencode`（`OPENCODE_CONFIG_DIR`） | `opencode-config` | `Unexpected error: FileSystem.writeFile (/home/node/.config/opencode/.gitignore)` |
| `/home/node/.opencode`（OpenCode state root） | `opencode-state` | `Unexpected error; Unknown: FileSystem.writeFile (/home/node/.opencode/.gitignore)` |

修好第一個不會順便修好第二個 —— 第二個是在第一個修完之後才浮出來的。

config dir 那個目錄**必須可寫**，裡面兩個設定檔**必須不可寫**，兩邊都要成立：設定正本用單檔
唯讀 mount 疊在可寫的 named volume 上，agent 才改不掉 provider 與 permission。
在 `config/opencode/` 新增設定檔時要一起在 `compose.yaml` 加一行單檔 mount，否則那個檔不會
進 container；`tests/static.sh` 會比對目錄內容與 mount 清單，漏了會擋下來。

`/home/node/.opencode` 沒有設定正本要疊，整個 named volume 可寫就好。不要改成把 `/home/node`
掛成可寫來一次解決，那會讓底下的唯讀設定、skills 與 snapshot mount 全部失效；`test ! -w
/home/node` 就是在擋這條捷徑。這個路徑不是 `/home/node/.openab`（OpenAB 自己的 state，bind 到
`runtime/openab`），兩個都要在。

`opencode-config` 與 `opencode-state` 只放 OpenCode 自己生成的檔。兩個都可以安全刪掉重建
（`docker volume rm work-agent_opencode-config work-agent_opencode-state`，container 停著時
做），設定不會跟著掉 —— 正本在 repo。`opencode-state` 重建會掉 OpenCode 自己的 state，不會掉
auth（那在 `opencode-data`）。

`gh` 這個 binary 本身存在於 OpenAB base image 內；這裡只確認 container 沒有提供 GitHub 憑證。

接著重做 local段落的 Slack測試，再確認未授權帳號的訊息不會被處理。

### Claude Code ACP specialist

OMO 的 `acpAgents.claude-code` 直接執行 Docker image 內固定版 `/usr/local/bin/claude-agent-acp`，
不會在 runtime 下載 package。Claude 只作 OMO specialist，不存在另一個 OpenAB deployment runtime，也沒有 rollback 操作。
登入 state 保存在 `claude-credentials` named volume；OpenAB config 永遠是 `config/openab.toml`。

### Host proxy 與防火牆

workspace agent（spec #21 §2、§4）的前置：host Caddy proxy 注入公司 gateway key，防火牆讓
prototype sandbox 只連得到外網、bot 只連得到 sandbox 的 runner port 與 proxy。兩者都不影響
現在的 bot（它還在 `work-agent_default` network、key 還在 `env/openab.env`），可以先裝。
這一段要 **sudo**，由維護者本人做。

| 檔案 | 作用 |
|---|---|
| `config/host-network.env` | network 名、subnet、bridge 名、bot 固定 IP、proxy／runner port 的正本 |
| `config/caddy/Caddyfile` | proxy 路由（只放 `POST /responses`，其餘 403）、bind 位址理由、JSON log 14 天 |
| `scripts/host-firewall.sh` | 規則與理由；`apply`／`check`／`remove` |
| `config/systemd/*.service` | 開機重下規則的 oneshot unit、Caddy unit |
| `scripts/install-host-network.sh` | 把上面這些裝到 host（`/etc/work-agent/`、`/usr/local/sbin/`、`/etc/systemd/system/`） |

位址（值見 `config/host-network.env`）：network `work-agent-net`（bridge `br-work-agent`，
`10.231.0.0/24`，`enable_icc=false`），proxy 綁 `10.231.0.1:18080`，bot 固定 `10.231.0.10`，
其他位址一律當 sandbox。

#### 安裝

```bash
cd "$HOME/code/work-agent-deploy"
./tests/static.sh                                   # host-network.py 要過
sudo ./scripts/install-host-network.sh install
```

`install` 是冪等的；改了上表任何一個檔案就重跑一次（host 上的副本不要手改）。它會：
下載 `config/versions.env` 釘版的 Caddy 並驗 SHA-512、從 `env/openab.env` 產生
`/etc/work-agent/gateway.env`（root 0600，已存在就不動）、建 `work-agent-net`、enable 並
重啟 `work-agent-firewall.service` 與 `work-agent-proxy.service`，最後跑一次 `check`。

`env/openab.env` 裡的 key 這一步**不會**拿掉，舊 bot 還要用；切換當晚才拿（spec #21 §12）。
`gateway.env` 只有三行：`COMPANY_GATEWAY_ORIGIN`、`COMPANY_GATEWAY_PATH_PREFIX`（由
`COMPANY_GATEWAY_BASE_URL` 拆出）、`COMPANY_GATEWAY_API_KEY`。換 key：刪掉 `gateway.env` 重跑
`install`，或直接 `sudoedit /etc/work-agent/gateway.env` 後 `sudo systemctl restart work-agent-proxy`。

確認：

```bash
sudo work-agent-firewall check                       # 沒有輸出、exit 0
cat /proc/sys/net/bridge/bridge-nf-call-iptables     # 要是 1
systemctl is-active work-agent-firewall work-agent-proxy
sudo iptables -S WORK-AGENT-FORWARD; sudo iptables -S WORK-AGENT-INPUT
```

`bridge-nf-call-iptables` 不是 1（或檔案不存在）時，同一個 bridge 上的流量不經過 iptables，
`enable_icc=false` 與 bot→runner 規則都不成立。Docker 建 `enable_icc=false` network 時會自己
載入 `br_netfilter`。**不可 `modprobe -r br_netfilter`**：它會連 `bridge` 一起卸掉，實測讓正式
bot 斷線約 7.5 小時。

#### 用拋棄式 container 驗（不動正式 bot）

兩個 container 扮演 bot 與 sandbox（bot 角色佔用 bot 的固定 IP，正式 bot 接上 `work-agent-net` 之後
就不能再這樣驗，要先停正式 bot），image 用 `config/versions.env` 釘的 sandbox base（有 curl 與
node）。bot 角色**不帶任何 env**，證明 key 不必進 container。

```bash
IMG=$(sed -n 's/^SANDBOX_NODE_IMAGE=//p' config/versions.env)
docker run -d --rm --name wa-fw-sandbox --network work-agent-net --dns 1.1.1.1 --dns 8.8.8.8 "$IMG" \
  node -e 'for (const p of [8080, 5173]) require("http").createServer((q, s) => s.end("ok " + p)).listen(p)'
docker run -d --rm --name wa-fw-bot --network work-agent-net --ip 10.231.0.10 "$IMG" sleep 3600
SANDBOX_IP=$(docker inspect -f '{{(index .NetworkSettings.Networks "work-agent-net").IPAddress}}' wa-fw-sandbox)
bot() { docker exec wa-fw-bot sh -c "$1"; }
sbx() { docker exec wa-fw-sandbox sh -c "$1"; }
probe() { echo "curl -sS -m 8 -o /dev/null -w '%{http_code}\n' $*"; }
```

逐條對 #25 的驗收：

```bash
# 1. bot 經 proxy 打 /responses 拿到正常回應；container env 沒有 key
bot "env | grep -c COMPANY_GATEWAY_API_KEY"          # 0
bot "curl -sS -m 120 http://10.231.0.1:18080/responses -H 'Content-Type: application/json' \
  -d '{\"model\":\"gpt-5.6-terra\",\"input\":\"reply with the single word ok\",\"store\":false}'"
                                                       # 回 JSON／SSE，含 ok；不是 401／403／405
# 2. 生圖 endpoint 打得通（同一個 /responses，會計費一次）；其他路徑 403
bot "curl -sS -m 180 -o /dev/null -w '%{http_code}\n' http://10.231.0.1:18080/responses \
  -H 'Content-Type: application/json' \
  -d '{\"model\":\"gpt-5.6-terra\",\"input\":\"a plain grey square\",\"tools\":[{\"type\":\"image_generation\",\"size\":\"1024x1024\",\"output_format\":\"png\"}],\"tool_choice\":{\"type\":\"image_generation\"},\"store\":false}'"
                                                       # 200
bot "$(probe http://10.231.0.1:18080/models)"           # 403
bot "$(probe -X POST http://10.231.0.1:18080/chat/completions)"   # 403
# 3. Caddy log 是 JSON，記得到路徑、status、耗時
sudo tail -n 4 /var/log/work-agent-proxy/access.log | jq -c '{uri: .request.uri, status, duration}'
sudo grep -cF "$(sudo sed -n 's/^COMPANY_GATEWAY_API_KEY=//p' /etc/work-agent/gateway.env)" \
  /var/log/work-agent-proxy/access.log               # 0：key 不進 log
# 4. sandbox 連得到外網；連不到 proxy、私有 IP 段、bot、IMDS（後面幾條都應該 timeout 或失敗，印 000）
sbx "$(probe https://registry.npmjs.org/)"             # 200
sbx "$(probe http://10.231.0.1:18080/responses)"        # 000（proxy）
sbx "$(probe http://10.231.0.10:8080/)"                 # 000（bot）
sbx "$(probe http://169.254.169.254/latest/meta-data/)" # 000（IMDS）
sbx "$(probe http://172.31.0.2/)"                       # 000（VPC）
sbx "$(probe http://172.17.0.1/)"                       # 000（host 的 docker0 位址）
# 5. bot 只連得到 sandbox 的 runner port
bot "$(probe http://$SANDBOX_IP:8080/)"                 # 200
bot "$(probe http://$SANDBOX_IP:5173/)"                 # 000
bot "$(probe http://10.231.0.1:22/)"                    # 000（host 其他 port）
docker rm -f wa-fw-bot wa-fw-sandbox
```

`000` 的意思是連線被丟掉（curl timeout），不是 HTTP 回應。第 4 項任何一條拿到 HTTP 碼都是規則沒生效。
`gpt-5.6-terra` 是 `agents/bin/company-image` 的 `MODEL`；換成 gateway 實際有的 model 也可以。

#### 重開機後仍在

```bash
sudo reboot
# 回來之後
sudo work-agent-firewall check && systemctl is-active work-agent-firewall work-agent-proxy
```

再跑一次上面第 4、5 項。`work-agent-firewall.service` 排在 `docker.service` **之前**
（必要時自己先建 `DOCKER-USER`，Docker 起來會沿用、不清內容），所以 `restart: unless-stopped`
的 sandbox 被拉起來時規則已經在了；Docker 重啟時它也會跟著重跑（`PartOf=docker.service`）。

#### IMDS hop limit（第二層）

主要靠防火牆擋 `169.254.169.254`；第二層是 EC2 metadata 的 hop limit 1，讓多一跳的 container
拿不到 IMDSv2 token。hop limit 只限制 IMDSv2 的 token，**IMDSv1 不受影響**，所以要一起設
`http-tokens required`。先確認 host 上沒有還在用 IMDSv1 的工具。需要有
`ec2:ModifyInstanceMetadataOptions` 權限的 AWS 憑證，在維護者自己的機器或 CloudShell 跑
（不要把 AWS 憑證放到 deployment host）：

```bash
aws ec2 modify-instance-metadata-options --region <region> --instance-id <instance-id> \
  --http-endpoint enabled --http-tokens required --http-put-response-hop-limit 1
aws ec2 describe-instances --region <region> --instance-ids <instance-id> \
  --query 'Reservations[0].Instances[0].MetadataOptions'
```

instance id 與 region 從 EC2 console 查。

#### 寫回與回滾

- 驗完把實際 bind 位址與 `sudo iptables -S WORK-AGENT-FORWARD`、`sudo iptables -S WORK-AGENT-INPUT`
  的輸出寫回 #21 §2。
- 回滾（不影響舊 bot）：

```bash
sudo systemctl disable --now work-agent-proxy work-agent-firewall
sudo work-agent-firewall remove
docker network rm work-agent-net          # 只有沒有 container 接在上面時
```

### 人工 release gate

`tests/static.sh` 是靜態檢查，`scripts/deploy.sh` 只驗到「container 起得來、runtime 版本對、
唯讀與可寫邊界成立」；preflight 另外只做 R2 secret／host 檢查和人工 checklist。**不會呼叫
Slack、Cloudflare、公司 gateway 或 STT，也不會證明 crontab 真的觸發過。**
沒有 Docker 的機器上 `tests/static.sh` 連 Compose render 都是本機解析（它會印 LIMITED VERIFICATION）。

所以下面這幾項是人工 gate，每次 release 都要重跑一遍，不能因為 `deploy.sh` 退出 0 就當它們過了：

| # | 要驗什麼 | 怎麼驗 | 為什麼自動化驗不到 |
|---|---|---|---|
| 0 | R2 lifecycle 與 Slack `files:read` | preflight 的 interactive checklist；逐項確認 R2 `incoming/` 1 天 lifecycle、Slack scope 並已 reinstall | 這兩項是外部人工設定；不以 API 代驗 |
| 1 | Slack 授權邊界 | 授權帳號 DM 有回應；未授權帳號沒有 | 需要真的 Slack workspace 與兩個身分 |
| 2 | Artifact 回原 thread | 上面「Local 首次啟動」第 6 項，含刻意失敗一次確認留檔 | 需要真的 `SLACK_BOT_TOKEN` 與真的 thread |
| 2a | Slack upload 的 body 編碼 | 同第 2 項，看檔案真的出現在 thread 而不是 `invalid_arguments` | 離線測試只比對 header 與 body 字串，不證明 Slack 收 |
| 3 | 生圖 | 下面那兩行 `company-image generate`（會產生一次計費呼叫） | 離線測試只驗 request shape |
| 4 | 文件附件流程 | 傳 PDF／DOCX／XLSX／PPTX，確認由 R2 URL 經 `parse-document <url> <filename>` 轉 Markdown；傳 ZIP 確認只列檔、傳 video 確認拒絕 | 需要真實 OpenAB attachment、R2 URL 與 Docling |
| 5 | Artifact cleanup 排程 | `./scripts/cleanup-artifacts.sh` 手動跑一次，再確認 crontab entry 與 log 沒有 `FAILED` | 靜態測試只證明排程被寫進去，不證明 cron 觸發過 |
| 6 | STT（`[stt].enabled = true` 時才要） | 用真實音檔打一次 `/audio/transcriptions` | 靜態檢查不發請求 |
| 7 | 模型推論打得到 gateway | 下面那行 `opencode run --pure`，要回文字而不是 HTTP 405 | `tests/provider-route.py` 只推導 endpoint，不發請求 |

第 5 項的兩個指令：

```bash
crontab -l | grep work-agent-artifact-cleanup
./scripts/cleanup-artifacts.sh
```

第 7 項。這是 `provider.company.npm` 曾經寫成 `@ai-sdk/openai-compatible` 時整台 bot 問不動的
那個檢查（回 HTTP 405，理由見
[ADR 0010](adr/0010-company-provider-uses-the-responses-api.md)）。改動 provider 設定、
`COMPANY_GATEWAY_BASE_URL` 或 OpenCode 版本之後都要重跑：

```bash
while IFS= read -r m; do
  docker compose -f ./compose.yaml exec -T backlog-agent \
    opencode run --pure --model "company/$m" 'reply with the single word ok' </dev/null
done < <(python3 -c 'import json; print("\n".join(json.load(open("config/opencode/opencode.json"))["provider"]["company"]["models"]))')
```

逐一驗證 provider 宣告的每個模型，避免文件另存一份可獨立修改的模型清單；gateway 少 expose
其中一個，只有實際推論會看得出來。

回文字就算過。回 HTTP 405 代表請求打到 `{baseURL}/chat/completions` 而 gateway 只收
`/responses` —— 先看 `provider.company.npm` 是不是 `@ai-sdk/openai`，再看
`COMPANY_GATEWAY_BASE_URL` 的前綴對不對。401／403 是 key 的問題，不是這條。

`deploy.sh` 結束時會把這份清單再印一次，避免只看終端機輸出的人以為已經驗完。

### 啟用 audio 轉錄（可選）

不要把 company gateway 當成 STT provider。先用一個已確認支援 OpenAI-compatible
`/audio/transcriptions` 的 endpoint 填 `STT_BASE_URL`、`STT_API_KEY`，再把 **兩份**
`config/openab*.toml` 的 `[stt].enabled` 改成 `true`，重新跑 `./tests/static.sh && ./scripts/deploy.sh`。
未做完這些步驟時 audio 必須被 bot 明確拒絕。首次驗證要以真實音檔打一次 STT endpoint；靜態檢查不會發請求。

## 4. 日常操作

不要直接在 deployment host修改repo檔案。所有設定與文件都先在local修改、commit及push；host只執行`git pull --ff-only`與部署命令。

更新部署：

```bash
cd "$HOME/code/work-agent-deploy"
git pull --ff-only
./tests/static.sh
./scripts/deploy.sh
```

查看 agent：

```bash
docker compose -f ./compose.yaml ps
docker compose -f ./compose.yaml logs -f --tail=200 backlog-agent
```

查看或立即更新 snapshots：

```bash
crontab -l | grep work-agent-snapshots
tail -n 50 "${XDG_STATE_HOME:-$HOME/.local/state}/work-agent/snapshots.log"
./scripts/update-snapshots.sh
```

查看或立即執行 artifact cleanup：

```bash
crontab -l | grep work-agent-artifact-cleanup
tail -n 50 "${XDG_STATE_HOME:-$HOME/.local/state}/work-agent/artifact-cleanup.log"
./scripts/cleanup-artifacts.sh
```

`cleanup-artifacts.sh` 自己不刪檔，它在 container 內執行
`/home/node/code/agent-config/skills/slack-list/scripts/slack-list cleanup`——刪除規則的正本在 agent-config，
host 端不要直接動 `runtime/drafts`。log 裡出現 `FAILED` 就是那一輪沒清到（多半是 container
沒在跑），artifact 會留到下一輪；持續 FAILED 要當成待處理，不是雜訊。

`update-snapshots.sh` 同步完會用 runtime image重建 CodeGraph索引，所以 host不需要安裝 Node。
索引放在 `$SNAPSHOT_ROOT/.index/<repo>`，每個 snapshot內的 `.codegraph` 是指過去的相對 symlink。
第一次部署時 image還沒 build，那一輪的索引會被跳過並印出訊息，`deploy.sh` 之後再跑一次同步即可。

停止 agent：

```bash
docker compose -f ./compose.yaml down
```

不要執行 `docker compose -f ./compose.yaml down -v`，它會刪掉 `claude-credentials`（Claude login）與
`opencode-data`（OpenCode auth 與 session storage）。`opencode-config` 與 `opencode-cache`
只有 OpenCode 自己生成的檔，刪掉會自己長回來。`runtime/` 不會被 `down` 刪除。

### 升級版本

版本正本只有 `config/versions.env` 一份。升級 OpenAB 時：

1. 取得新的 OpenAB multi-arch digest。
2. 更新 `OPENAB_VERSION`、`OPENAB_IMAGE`、`已移除的第二個 image`。
3. 確認 Dockerfile 自包的 `opencode-ai` exact version，同步更新 `OPENCODE_VERSION` ——
   它會安裝到獨立的 `/opt/opencode-${OPENCODE_VERSION}` prefix，並在 build 時驗證
   `/usr/local/bin/opencode` 的 resolved path 與 exact version；對不上會失敗，這是刻意的。
4. 確認 Claude Code CLI 的 exact version，更新 `CLAUDE_CODE_VERSION`；Dockerfile 會固定安裝它。
5. `./tests/static.sh && ./scripts/deploy.sh`。

升級 OMO 時只改 `OMO_VERSION`，並把 `config/opencode/opencode.json` 的 plugin pin 與
`config/opencode/oh-my-opencode-slim.json` 的 `$schema` 改成同一版；三處不一致 static test 會擋。

### 更新或新增 Skill

`/home/node/.claude/skills` 是 `agent-config/skills` 整個目錄的 read-only mount。`agent-config` 是 skill 與
MCP 定義的 canonical public catalog，由 skillshare 管理；更新它只會改變 snapshot 裡可讀到的 catalog，
不會自動新增這個 bot 的 runtime capability。要讓 bot 使用新能力，必須先 review 本 repo 的 OpenCode
permission、OMO/runtime 設定、`agents/CLAUDE.md` contract 與 Slack Home，再依需要部署；不要把 catalog
可見性當成 authorization。skill 怎麼安裝、更新第三方來源，看 agent-config 自己的 README。

要立即生效就在 host上執行 `./scripts/update-snapshots.sh`。

部署層不裁這份 catalog：整個目錄都掛進去，Claude runtime 當它是 personal level skill，OpenCode
由 `config/opencode/opencode.json` 的 `skills.paths` 指過去。**讀得到不等於 permission allow，也不等於
contract 允許執行**；runtime authorization 以本 repo 設定與 `agents/CLAUDE.md` 為準，不在 runbook 另列
一份可獨立修改的 skill allowlist。

`slack-list` 設定只讀環境變數：container 從 `env/openab.env` 拿到，不需要 `~/.config/slack-list/.env`。

從 work-helper 切過來的 host，snapshot root 裡還留著舊的 `work-helper` 目錄，`preflight.sh` 會因為它不在
`config/repos.conf` 而失敗。確認新的 `agent-config` snapshot 同步成功後，刪掉 `$SNAPSHOT_ROOT/work-helper`
與 `$SNAPSHOT_ROOT/.index/work-helper`。

需要改這個 repo的情況包括：要讓 catalog 中的新能力成為 bot capability、它需要新的 runtime permission／mount，
或它的行為會撞到 Slack 回覆規則。這時要同步 review 本 repo 的 provider／permission／mount、
`agents/CLAUDE.md` 的 contract 與 Slack Home；不要只同步 catalog，否則 agent 可能在 Slack 上嘗試後失敗。

已啟動的Claude session可能已把舊skill內容讀進context。同步後使用新的Slack thread或等待session回收再驗證，不用以舊session判斷同步失敗。

### 新增唯讀 Repo

新增repo會擴大product-context bot可讀資料的範圍，必須當成能力與權限變更review。整個 snapshot root是單一 mount，所以只改一個檔案：

| 正本 | 要改什麼 |
|---|---|
| `config/repos.conf` | 新增一行 `name\|SSH remote\|基準branch` |

Snapshot key對應的GitHub帳號必須先取得新repo的read權限。Push deployment repo後，在host執行：

```bash
git pull --ff-only
./scripts/update-snapshots.sh
./scripts/preflight.sh
./scripts/deploy.sh
```

`preflight.sh` 會擋下 snapshot root裡出現不在 `config/repos.conf` 的目錄，避免有東西被掛進 container卻沒有經過review。

最後依「驗證安全邊界」確認所有repo snapshot不可寫、drafts可寫，並從新的Slack thread詢問新repo內容。
