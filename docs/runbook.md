# Work Agent Runbook

Local與deployment host共用同一份 Compose及runtime設定。State、artifact 暫存檔、thread workspace 與 sandbox
產物都放在本 repo的 `runtime/`；兩邊都要建立 root `.env`，指定 snapshot root與執行 docker的使用者 uid/gid。

Deployment host是一台 Linux host，Compose以維護者自己的帳號執行。該帳號必須在`docker` group裡，
並且host要有Git、SSH client、Python 3、curl、jq與cron。host proxy 與防火牆要 **sudo** 安裝（見
「Host proxy 與防火牆」）。SELinux enforcing的host不需要額外設定，Compose的bind mount已經帶`:z`。

所有 compose 指令都可以直接用 `docker compose -f ./compose.yaml`：image digest 與 build args 已寫在
`compose.yaml`，正本是 `config/versions.env`，`tests/static.sh` 比對兩邊。

Compose service 仍然叫 `backlog-agent`，container 叫 `work-agent`。這是識別字，不是產品定位；
改名會讓現有部署的 volume 與 crontab 對不上，所以沒有跟著文件一起改。兩格 prototype sandbox 是
`prototype-sandbox-1`、`prototype-sandbox-2`（container `work-agent-sandbox-1`、`work-agent-sandbox-2`）。

## 1. 設定 Slack App

這只需做一次，local與deployment host共用。

1. 開啟 **Socket Mode**。
2. 建立 App-Level Token，scope選 `connections:write`，取得 `xapp-...`。
3. 開啟 **Event Subscriptions**，訂閱 bot events：`app_mention`、`message.groups`、`message.im`。
4. 到 **App Home** 開啟 **Messages Tab**，並允許使用者從 Messages Tab傳送訊息。
5. bot token scopes 只有這 10 個：

```text
app_mentions:read
chat:write
files:read
files:write
groups:history
im:history
lists:read
lists:write
reactions:write
users:read
```

6. 安裝到 workspace，取得 `xoxb-...`。
7. 只把 app邀進 `C0BPZRN6H3R` 與 `C0B9PSESQ2U`。

**加 scope** 可以 **Reinstall to Workspace**；**拿掉 scope** 不行 —— Slack 的 Reinstall 不會降 scope，
一定要先 `auth.revoke` 或 uninstall 再裝。token 會換新、bot 會被移出所有 channel，要照第 7 步重新
邀回。`chat:write.public`、`channels:manage`、`groups:write.invites`、`groups:read` 都不該在清單裡；
List 留言串讀不到時要補的是 `*:history`，不是 `groups:read`。

Socket Mode用 WebSocket接收 events，所以不用填 Event Subscriptions的 Request URL。OpenAB使用
`xoxb-...` 透過 Slack Web API回覆訊息。（告警用的 Incoming Webhook 在另一個 Slack app，見「清理與告警排程」。）

`users:read` 讓發布腳本用 bot token 呼叫 [`users.list`](https://api.slack.com/methods/users.list)，逐頁列出
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

同一個 Slack app同時只能跑一個 OpenAB instance；local測試前先停止正式 instance。bot 要接上 external
network `work-agent-net`、經 host proxy 打 gateway，所以 local 也要先照「Host proxy 與防火牆」裝好
（要 sudo）。

Local不掛開發用的 checkout，而是自己建一份乾淨 snapshot，避免把各 repo的 `.env` 掛進 container：

```bash
cd "$HOME/code/work-agent"

test -f .env || cp .env.example .env
sed -i "s/^HOST_UID=.*/HOST_UID=$(id -u)/; s/^HOST_GID=.*/HOST_GID=$(id -g)/" .env

test -f env/openab.env || cp env/openab.env.example env/openab.env
chmod 600 env/openab.env
$EDITOR env/openab.env

# Local用自己的 GitHub key即可，不必另外產一把。
GITHUB_SSH_KEY="$HOME/.ssh/<你的 GitHub key>" ./scripts/update-snapshots.sh
mkdir -p runtime/openab runtime/drafts runtime/work runtime/out
stat -c '%n %u:%g' runtime/work runtime/out   # 兩個都要是 1000:1000（sandbox 的 node uid 固定 1000）

./tests/static.sh
./scripts/preflight.sh
docker compose -f ./compose.yaml build --pull
docker compose -f ./compose.yaml up -d
docker compose -f ./compose.yaml ps
docker compose -f ./compose.yaml logs --tail=100 backlog-agent
```

`runtime/work`、`runtime/out` 不存在時 Docker 會自己建成 root 擁有，bot 與 sandbox 都寫不進去；
`preflight.sh` 會擋。

`env/openab.env` 至少要填入 `SLACK_BOT_TOKEN`、`SLACK_APP_TOKEN`、`SLACK_LIST_ID`、
`SLACK_TEAM_ID`、`SLACK_WORKSPACE_URL`（`slack-list` 與 `publish-slack-home.sh` 會用，缺了會直接中止）
與 R2 credentials，並保留：

```text
WORK_HELPER_ISSUE_MODE=manual
```

**公司 gateway key 不放這裡。** `env_file` 會把整份檔案載進 bot container；key 只在 host proxy 的
`/etc/work-agent/gateway.env`（見「Host proxy 與防火牆」），`preflight.sh` 看到 `COMPANY_GATEWAY_API_KEY`
會拒絕部署。bot 內的 `COMPANY_GATEWAY_BASE_URL` 是 proxy 位址，寫在 `compose.yaml`。

模型 ID 在 `config/codex/config.toml`，必須對得上公司 gateway 實際 expose 的名稱，且等於
`config/codex/catalog.json` 唯一那筆的 `slug`（`tests/static.sh` 會比對）。

Root `.env` 只給 Compose用（snapshot root與 uid）；`env/openab.env` 才會傳進 container。
`.env`、`env/openab.env` 與 `runtime/` 都不進 Git。Codex 的 session 存在 named volume `codex-home`，
重建 container後仍保留。

### Cloudflare R2 filestore（deployment host 手動前置）

OpenAB config 的 `[filestore]`：bucket `work-agent-attachments`，endpoint
`https://99de68928da234ebcf0c9370443ad7ee.r2.cloudflarestorage.com`，region `auto`，
prefix `incoming/`，presigned URL TTL 3600 秒，單檔上限 **50 MiB**。設定檔不含
任何 R2 credential；請在 deployment host 手動編輯未追蹤的 `env/openab.env`，加入：

```text
R2_ACCESS_KEY_ID=<Cloudflare R2 access key>
R2_SECRET_ACCESS_KEY=<Cloudflare R2 secret key>
```

完成後確認檔案權限仍是 `600` 或 `640`。Cloudflare R2 bucket
`work-agent-attachments` 還必須手動建立 lifecycle rule，讓 `incoming/` 物件 **1 天後
過期**；這個外部設定不在 repo 內，也不會被 static test 或 API 驗證。

`preflight.sh` 會拒絕缺少／placeholder 的 R2 secrets、錯誤 endpoint，並在繼續前要求人工逐項確認
R2 lifecycle 與 Slack `files:read`；不會嘗試呼叫 Cloudflare 或 Slack API。沒有 interactive TTY 的
`preflight.sh`／`deploy.sh` 會安全中止。

#### 文件附件支援矩陣

OpenAB 把 R2 `incoming/` presigned URL 與原始 filename 注入 prompt；agent 原樣交給
`sandbox-run --thread <channel_id> <thread_ts> -- sandbox-attachment <url> <filename>`，由 prototype
sandbox 下載、驗檔頭、以 `npm exec` 現裝 `config/versions.env` 釘的 officeparser 轉成 Markdown，寫到
`runtime/out/<channel_id>-<thread_ts>/<檔名>.md`。bot 本身沒有解析工具。

| 輸入類型 | 狀態 | 處理方式與限制 |
|---|---|---|
| Slack 文字訊息 | 支援 | OpenAB 直接注入 prompt。 |
| 圖片 | 支援 | OpenAB 原生 media 路徑交給模型。 |
| PDF | 支援文字型 PDF | sandbox 轉 Markdown；不做 OCR，掃描檔轉不出字就明講讀不到。 |
| DOCX、XLSX、PPTX | 支援 | sandbox 轉 Markdown（XLSX 轉成 Markdown table）。 |
| ZIP、CSV、`.doc`、`.xls`、`.ppt` | 不支援 | `sandbox-attachment` 只接受 PDF 與 OOXML，其他回 exit 2。 |
| audio | 不支援 | STT 未啟用；不轉錄。 |
| video | 不支援 | 不下載、轉檔或解析。 |

所有文件單檔上限 50 MiB；下載 120 秒、轉換 600 秒上限（`sandbox/sandbox-attachment` 檔頭）。

在 Slack確認：

1. DM詢問一個 repo問題。
2. 在 `#你為什麼不問問神奇海螺ㄋ` @ bot建立一筆實際要保留的待辦，確認指派給sender並保存來源。
3. 再次要求建立同名待辦，確認沒有新增第二列。
4. 在待辦列的 item留言串 @ bot，確認它能找到正確的 `Rec...`。
5. 傳一張截圖問問題，確認圖片有被讀到；再傳 PDF、DOCX、XLSX 或 PPTX，確認 bot 經 sandbox 轉
   Markdown 後回答；傳 ZIP 與 video 確認明確回覆不支援。
6. 要一個 app prototype（例如「把某個按鈕改成紅色給我看」），確認 bot 建了 thread workspace、經
   `sandbox-run` 截圖，PNG 回到同一個 thread；再要一個只演示流程的 HTML prototype，確認單一 HTML
   檔回到同一個 thread。
7. 要一張圖，確認 bot 用 `company-image` 產出 PNG，再用 `slack-thread-artifact` 回同一個 thread，
   drafts 原檔被刪；讓 Slack API 故意失敗一次，確認原檔保留給 cleanup。
   這一項也是 Slack upload body 編碼唯一的真實驗證：`files.getUploadURLExternal` 收到 JSON body 會回
   `invalid_arguments`，檔案完全傳不出去。stderr 出現 `invalid_arguments` 就是編碼壞了，不是 token 或
   scope 的問題（見 [`adr/0009`](adr/0009-thread-artifact-upload-and-optional-stt.md)）。

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

順序有意義：snapshot 與 `runtime/` 目錄要在 `deploy.sh` 之前就緒（preflight 會擋），crontab 要等新
container 起來之後才裝 —— 先裝的話，清理與告警排程會對還不存在的 sandbox 與 bot 跑 `exec`，並發出
「container 不在」告警。

```bash
cd "$HOME/code/work-agent"

test -f .env || cp .env.example .env
sed -i "s/^HOST_UID=.*/HOST_UID=$(id -u)/; s/^HOST_GID=.*/HOST_GID=$(id -g)/" .env

test -f env/openab.env || cp env/openab.env.example env/openab.env
chmod 600 env/openab.env
$EDITOR env/openab.env       # 暫時加上 COMPANY_GATEWAY_BASE_URL 與 COMPANY_GATEWAY_API_KEY，下一段要用

./scripts/update-snapshots.sh  # 第一次同步；image 還沒 build，索引這一輪會跳過
mkdir -p runtime/openab runtime/drafts runtime/work runtime/out
stat -c '%n %u:%g' runtime/work runtime/out   # 兩個都要是 1000:1000

# 「Host proxy 與防火牆」的「安裝」：sudo install、確認 gateway.env、再從 env/openab.env 刪掉 key。

./scripts/deploy.sh
docker compose -f ./compose.yaml logs --tail=100 backlog-agent
./scripts/install-sync-cron.sh  # 新 container 起來之後才跑；會再同步一次並建好索引
```

`install-sync-cron.sh` 寫入的排程見「清理與告警排程」。告警 webhook 也照那一節設定。

`deploy.sh` 會先執行 preflight，再 build及啟動 container，最後印出它**沒有**驗到的項目（見下面
「人工 release gate」）。

### 驗證安全邊界

```bash
docker compose -f ./compose.yaml ps
crontab -l | grep work-agent-
docker compose -f ./compose.yaml exec backlog-agent sh -lc \
  'python3 --version >/dev/null &&
   git --version >/dev/null &&
   /home/node/code/agent-config/skills/slack-list/scripts/slack-list --help >/dev/null &&
   test -w /home/node/.openab &&
   test -w /home/node/drafts &&
   test -w /home/node/work &&
   test ! -w /home/node/out &&
   test ! -w /home/node/code/teamsync-frontend &&
   test ! -w /home/node/code/teamsync-backend &&
   test -d /home/node/.agents/skills/slack-list && test ! -w /home/node/.agents/skills &&
   test ! -w /home/node &&
   test ! -e /home/node/.ssh &&
   ! command -v gh >/dev/null &&
   ! env | grep -q "^COMPANY_GATEWAY_API_KEY=" &&
   command -v company-image >/dev/null &&
   command -v slack-thread-artifact >/dev/null &&
   command -v thread-workspace >/dev/null &&
   command -v sandbox-run >/dev/null &&
   test ! -w /usr/local/bin/company-image &&
   codegraph explore boot -p /home/node/code/agent-config >/dev/null'
```

`test -w` 這幾項是 SELinux label 與 uid 都正確才會過的，Compose render 成功不代表通過。
`deploy.sh` 另外驗 codex／codex-acp 版本、`CODEX_HOME` 可寫而上面的 `config.toml`、`AGENTS.md` 不可寫。

sandbox 那邊：

```bash
docker inspect -f '{{.Name}} privileged={{.HostConfig.Privileged}} {{range .Mounts}}{{.Destination}}:{{.RW}} {{end}}' \
  work-agent-sandbox-1 work-agent-sandbox-2   # privileged=false；/work:false、/out:true；沒有 docker.sock
docker compose -f ./compose.yaml exec prototype-sandbox-1 sh -c 'env | grep -cE "^(SLACK|COMPANY|R2|STT)_"'   # 0
```

生圖那條路徑要真的打一次公司 gateway 才算驗過（會產生一次計費呼叫）：

```bash
docker compose -f ./compose.yaml exec backlog-agent \
  company-image generate --prompt 'a plain grey square' --name smoke
docker compose -f ./compose.yaml exec backlog-agent sh -lc \
  'head -c 8 "$(ls -t /home/node/drafts/*/smoke*.png | head -1)" | od -c | head -1'
sg work-agent-proxy -c 'tail -n 3 /var/log/work-agent-proxy/access.log' | jq -c '{uri: .request.uri, status, ua: .request.headers["User-Agent"]}'
```

第一行會印出 drafts 內的 PNG 路徑，第二行確認開頭是 PNG magic，第三行確認那筆請求經過 proxy
（User-Agent 是 `work-agent-company-image`）。驗完把那個檔刪掉。`tests/image-runtime.py` 只驗請求
shape、兩種回應形狀與拒絕不合法輸入，**不會**證明公司 gateway 接受這個請求。

接著重做 local段落的 Slack測試。

### Host proxy 與防火牆

host Caddy proxy 注入公司 gateway key，防火牆讓 prototype sandbox 只連得到外網、bot 只連得到
sandbox 的 runner port 與 proxy（理由見 [ADR 0013](adr/0013-codex-reaches-the-gateway-through-a-host-proxy.md)、
[ADR 0014](adr/0014-run-repo-code-in-the-prototype-sandbox.md)）。這一段要 **sudo**，由維護者本人做。

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
cd "$HOME/code/work-agent"
./tests/static.sh                                   # host-network.py 要過
sudo ./scripts/install-host-network.sh install
```

`install` 是冪等的；改了上表任何一個檔案就重跑一次（host 上的副本不要手改）。它會：
下載 `config/versions.env` 釘版的 Caddy 並驗 SHA-512、從 `env/openab.env` 產生
`/etc/work-agent/gateway.env`（root 0600，已存在就不動）、建 `work-agent-net`、enable 並
重啟 `work-agent-firewall.service` 與 `work-agent-proxy.service`，最後跑一次 `check`。

`gateway.env` 只有三行：`COMPANY_GATEWAY_ORIGIN`、`COMPANY_GATEWAY_PATH_PREFIX`（由
`COMPANY_GATEWAY_BASE_URL` 拆出）、`COMPANY_GATEWAY_API_KEY`。換 key：刪掉 `gateway.env` 重跑
`install`，或直接 `sudoedit /etc/work-agent/gateway.env` 後 `sudo systemctl restart work-agent-proxy`。

`install` **不會**把 key 從 `env/openab.env` 拿掉。確認 `gateway.env` 跟 `env/openab.env` 拆出來的一樣，
再自己刪掉 `env/openab.env` 的 `COMPANY_GATEWAY_API_KEY` 那一行（`COMPANY_GATEWAY_BASE_URL` 留著無妨，
`compose.yaml` 會蓋過它）：

```bash
./scripts/install-host-network.sh gateway-env env/openab.env | sudo cmp -s - /etc/work-agent/gateway.env && echo same
sed -i '/^COMPANY_GATEWAY_API_KEY=/d' env/openab.env
```

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

#### 用拋棄式 container 驗

兩個 container 扮演 bot 與 sandbox。bot 角色佔用 bot 的固定 IP，所以正式 bot 在 `work-agent-net` 上時
要先停正式 bot 才能這樣驗。image 用 `config/versions.env` 釘的 sandbox base（有 curl 與 node）。bot 角色
**不帶任何 env**，證明 key 不必進 container。

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

#### 拆除

只在不再需要新架構時（例如回滾後觀察期結束決定不切了）。舊 bot 不在 `work-agent-net`、key 在
`env/openab.env`，拆除不影響它：

```bash
sudo systemctl disable --now work-agent-proxy work-agent-firewall
sudo work-agent-firewall remove
docker network rm work-agent-net          # 只有沒有 container 接在上面時
```

### 清理與告警排程

全部由 `scripts/lib.sh` 的 `render_crontab` 產生、`./scripts/install-sync-cron.sh`
一起寫進 crontab；刪除一律 `docker compose exec` 進擁有該目錄的 container 做，不從 host 直接刪。

| Marker | 時間 | 做什麼 | log（`~/.local/state/work-agent/`） |
|---|---|---|---|
| `# work-agent-snapshots` | 每小時 :00 | `scripts/update-snapshots.sh`：同步 snapshot、重建 CodeGraph 索引 | `snapshots.log` |
| `# work-agent-artifact-cleanup` | 每小時 :17 | bot 內 `slack-list cleanup`（drafts 24h）＋ 刪 `CODEX_HOME/sessions` 超過 7 天的 `rollout-*.jsonl`（`log/`、sqlite 不動）＋ 量 `CODEX_HOME` | `artifact-cleanup.log` |
| `# work-agent-workspace-cleanup` | 每小時 :27 | bot 內 `thread-workspace cleanup`（7 天／20 GB，被刪的 thread 收到通知）＋ sandbox 內 `sandbox-runner cleanup-out`（out 24h） | `workspace-cleanup.log` |
| `# work-agent-pnpm-prune` | 週日 04:37 | sandbox 內用各 snapshot repo 釘的 pnpm 版本跑 `pnpm store prune` | `pnpm-prune.log` |
| `# work-agent-alerts` | 每小時 :47 | `scripts/alerts.py`：四類告警，發到維護者私人 channel | `alerts.log` |

`install-sync-cron.sh` 一次裝全部 5 行：只裝同步、不裝清理的 host 會讓上傳失敗的檔、thread workspace
與 rollout 無限長大。drafts 的保留期限是 24 小時，但檢查每小時才一次，所以實際壽命是 **24 到 25 小時**。
**只在新 container 起來之後才跑它**（見「安裝」）。

告警只送 Slack Incoming Webhook，不經 bot。首次部署（或換 webhook）時維護者本人做：

1. 在 agent-runner 那個 Slack app 的 **Incoming Webhooks** 另加一個 webhook，channel 選自己的私人
   channel；跟 agent-runner 原本那個分開，外流時只撤這一個。
2. URL 寫到 deployment host 上 cron 使用者的檔案，**不要**放進 repo、`.env` 或 `env/openab.env`：

   ```bash
   install -d -m 0700 ~/.config/work-agent
   (umask 077 && read -rsp 'webhook URL: ' url && printf '%s\n' "$url" > ~/.config/work-agent/alert-webhook)
   ```

   權限比 0600 寬，`alerts.py` 會拒絕送出。路徑要換就在 repo 的 `.env` 設
   `WORK_AGENT_ALERT_WEBHOOK_FILE`。
3. 讓 cron 使用者讀得到 host proxy 的 access log：重跑 `sudo ./scripts/install-host-network.sh install`。
   它會建 system user `work-agent-proxy`、把執行 sudo 的使用者加進 `work-agent-proxy` group，
   並把既有 log 調成 0640。確認：

   ```bash
   id -nG | tr ' ' '\n' | grep -x work-agent-proxy     # 新的登入 shell 才看得到；cron 不必重登
   sg work-agent-proxy -c 'tail -n1 /var/log/work-agent-proxy/access.log' | jq -c .request.uri
   ```

4. `./scripts/install-sync-cron.sh` 重裝 crontab，`crontab -l | grep work-agent-` 應該有 5 行。
5. 手動跑一次 `./scripts/alerts.sh`，`alerts.log` 最後一行是 `no alerts` 或 `delivered N alert(s)`；
   出現 `FAILED` 就是 webhook 檔或 log 權限沒弄好。

告警內容與門檻見 `scripts/alerts.py` 檔頭。`/responses` 用量告警在 log 累積滿 7 天前只記錄；
`thread workspace`／`CODEX_HOME` 大小超過 3 小時沒回報本身也會告警（代表清理排程沒在跑）。

### 人工 release gate

`tests/static.sh` 是靜態檢查，`scripts/deploy.sh` 只驗到「container 起得來、runtime 版本對、
唯讀與可寫邊界成立、key 不在 container」；preflight 另外只做 secret／目錄檢查和人工 checklist。**不會呼叫
Slack、Cloudflare、公司 gateway 或 STT，不會跑真的 sandbox job，也不會證明 crontab 真的觸發過。**
沒有 Docker 的機器上 `tests/static.sh` 連 Compose render 都是本機解析（它會印 LIMITED VERIFICATION）。

所以下面這幾項是人工 gate，每次 release 都要重跑一遍，不能因為 `deploy.sh` 退出 0 就當它們過了。
`tests/static.sh` 與 `scripts/deploy.sh` 退出 0 不算驗過其中任何一項。全部過了才 publish Slack Home。

| # | 要驗什麼 | 怎麼驗 | 為什麼自動化驗不到 |
|---|---|---|---|
| 0 | R2 lifecycle 與 Slack `files:read` | preflight 的 interactive checklist | 外部人工設定；不以 API 代驗 |
| 1 | Slack scope 與 channel | List 留言串所在 channel 需要的 history scope、兩個 channel 是否私有；revoke 重裝後 List 分享、App Home、xapp token 是否保留；workspace 有沒有開 app approval。在「切換與回滾」第 3 步、用舊 bot 跑新 token 驗 | 需要真的 Slack workspace |
| 2 | Slack 授權邊界 | 授權帳號 DM 有回應；不在 app 事件範圍內的訊息沒有 | 需要真的 Slack workspace 與兩個身分 |
| 3 | 經 OpenAB 的真實往返 | 從 Slack 問一題（一般對話、streaming、一個 shell tool call）；同一 thread 再 @ 一次確認 `session/load` 續接；回覆開頭沒有 model metadata warning；Caddy log 有對應的 `/responses` | 離線只驗過最小 ACP client；OpenAB 本身沒跑過 |
| 4 | 生圖經 proxy | 「驗證安全邊界」那三行 `company-image generate`（會計費一次），且出現在 Caddy log | 離線測試只驗 request shape |
| 5 | 經 `/proc` 讀不讀得到 OpenAB 的 secret | 下面那行；照實記下讀不讀得到，寫回 `docs/system-design.md`「Runtime secret」 | 只是依 `/proc` 權限推論 |
| 6 | thread workspace → sandbox 截圖 | 「Local 首次啟動」Slack 確認第 6 項；`docker inspect -f '{{.State.OOMKilled}}' work-agent-sandbox-1 work-agent-sandbox-2` 都是 `false`（5g 撐得過 install 與 dev server 預打包）；反覆改 Tailwind 幾次 vite 不 heap OOM | 要真的 repo、真的 install 與 Chromium |
| 7 | 附件轉換 | 「Local 首次啟動」Slack 確認第 5 項：真的 PDF／DOCX／XLSX／PPTX 經 R2 URL → sandbox 轉 Markdown | 需要真實 OpenAB attachment 與 R2 URL |
| 8 | Artifact 回原 thread | 「Local 首次啟動」Slack 確認第 6、7 項：drafts、work（HTML）、out（截圖）都上傳到真的 Slack；刻意失敗一次確認 drafts 留檔；檔案真的出現在 thread 而不是 `invalid_arguments` | 離線測試只比對 header 與 body 字串 |
| 9 | 防火牆 | 「用拋棄式 container 驗」第 4、5 項（要先停正式 bot），或至少 `sudo work-agent-firewall check`；重開機後仍在；IMDS hop limit 1＋`http-tokens required` | 規則在 host kernel，不在 repo |
| 10 | 清理與告警排程 | 下面那幾行：各 log 沒有 `FAILED`；告警真的送到維護者私人 channel | 靜態測試只證明排程被寫進去 |
| 11 | STT（`[stt].enabled = true` 時才要） | 用真實音檔打一次 `/audio/transcriptions` | 靜態檢查不發請求 |
| 12 | 回滾前提 | `docker image inspect work-agent-openab:pre-workspace` 成功；切換前的 commit 已記下 | 只在切換那次需要 |

第 5 項（以 node 身分，跟 agent 同 uid）：

```bash
docker compose -f ./compose.yaml exec -T backlog-agent sh -c \
  'for e in /proc/[0-9]*/environ; do tr "\0" "\n" <"$e" 2>/dev/null | grep -q "^SLACK_APP_TOKEN=" && echo "readable: $e"; done; true'
```

第 10 項：

```bash
crontab -l | grep work-agent-
./scripts/cleanup-artifacts.sh && ./scripts/cleanup-workspaces.sh && ./scripts/alerts.sh
tail -n 5 ~/.local/state/work-agent/{artifact-cleanup,workspace-cleanup,alerts}.log
```

手動跑過之後，隔一個整點再看一次 log 有新的一輪，才算 cron 真的觸發過。

`deploy.sh` 結束時會把未驗清單再印一次，避免只看終端機輸出的人以為已經驗完。

### 啟用 audio 轉錄（可選）

不要把 company gateway 當成 STT provider。先用一個已確認支援 OpenAI-compatible
`/audio/transcriptions` 的 endpoint 填 `STT_BASE_URL`、`STT_API_KEY`，再把
`config/openab.toml` 的 `[stt].enabled` 改成 `true`，重新跑 `./tests/static.sh && ./scripts/deploy.sh`。
未做完這些步驟時 audio 必須被 bot 明確拒絕。首次驗證要以真實音檔打一次 STT endpoint；靜態檢查不會發請求。

## 4. 切換與回滾

從唯讀 product-context bot 切到 workspace agent。同一晚、原地切換（同一個 Slack app、同一台 host，
新舊 bot 不同時跑）、不事先公告。需要維護者本人：revoke 重裝 Slack app、sudo。

前提（可以先做、不影響舊 bot）：「Host proxy 與防火牆」已安裝、`/etc/work-agent/gateway.env` 已產生；
告警 webhook 檔已建好。

### 切換

1. 保留舊 image 與 commit：

   ```bash
   cd "$HOME/code/work-agent"
   docker tag work-agent-openab:0.10.0-opencode work-agent-openab:pre-workspace
   git rev-parse HEAD | tee ~/work-agent-pre-workspace.commit
   ```

2. 停舊 bot（`docker compose -f ./compose.yaml stop backlog-agent`）→ Slack app `auth.revoke`／uninstall →
   用「設定 Slack App」的 10 個 scope 重裝，把新 `xoxb-` 寫進 `env/openab.env` → 邀回那 2 個 channel。
3. **舊 bot 跑新 token**（`docker compose -f ./compose.yaml up -d`，仍是舊 commit），驗 gate 第 1 項與：
   `slack-list context`／`add`、在 List 留言串 @bot、`./scripts/publish-slack-home.sh`。沒過就當晚停在
   這裡、不換 container，先修 scope（補 `*:history`）。
4. merge 實作 PR，`git pull --ff-only`，然後：

   ```bash
   ./scripts/install-host-network.sh gateway-env env/openab.env | sudo cmp -s - /etc/work-agent/gateway.env && echo same
   sed -i '/^COMPANY_GATEWAY_API_KEY=/d' env/openab.env      # 上一行印 same 才刪
   mkdir -p runtime/work runtime/out && stat -c '%n %u:%g' runtime/work runtime/out   # 1000:1000
   ./scripts/deploy.sh
   ./scripts/install-sync-cron.sh                            # 新 container 起來之後才裝
   ```

5. 跑「人工 release gate」全部項目。全部過了才 `./scripts/publish-slack-home.sh` 發新 Home；Home 本身就是
   對 PM 的告知。

切換前的 thread 不延續：再 @bot 會開新 Codex session，看不到之前的對話；不寫轉換程式。

### 回滾

- **條件**：人工 gate 任一條沒過，或 PM 回報核心流程（問產品問題、做 prototype、待辦）不能用、當天又
  修不好。維護者人工判斷，不設自動回滾。
- **步驟**：

  ```bash
  cd "$HOME/code/work-agent"
  docker compose -f ./compose.yaml down                     # 新 bot 與兩格 sandbox；不要加 -v
  # 把 key 貼回 env/openab.env（舊 bot 從 env_file 讀 key；COMPANY_GATEWAY_BASE_URL 切換時沒刪，還在）
  sudo grep '^COMPANY_GATEWAY_API_KEY=' /etc/work-agent/gateway.env >> env/openab.env
  grep -c '^COMPANY_GATEWAY_BASE_URL=' env/openab.env       # 1
  git checkout "$(cat ~/work-agent-pre-workspace.commit)"
  docker tag work-agent-openab:pre-workspace work-agent-openab:0.10.0-opencode
  docker compose -f ./compose.yaml up -d --no-build
  # 舊版 install-sync-cron.sh 只認得自己那 2 個 marker；新增的 3 個要手動拿掉，否則會對不存在的 sandbox 跑並告警
  crontab -l | grep -vE '# work-agent-(workspace-cleanup|pnpm-prune|alerts)$' | crontab -
  ```

  沿用新 token：10 個 scope 全是舊 bot 在用的，舊 token 不用留。舊 bot 的 volume（`work-agent_opencode-*`、
  `work-agent_claude-credentials`）新版 `compose.yaml` 刻意不宣告，`down` 不會刪掉它們。
- **觀察期** 14 天（跟 Caddy log 保留天數相同），結束後才刪 `:pre-workspace` image 與
  `work-agent_opencode-*`、`work-agent_claude-credentials` volume。

## 5. 日常操作

不要直接在 deployment host修改repo檔案。所有設定與文件都先在local修改、commit及push；host只執行`git pull --ff-only`與部署命令。

更新部署：

```bash
cd "$HOME/code/work-agent"
git pull --ff-only
./tests/static.sh
./scripts/deploy.sh
```

查看 agent：

```bash
docker compose -f ./compose.yaml ps
docker compose -f ./compose.yaml logs -f --tail=200 backlog-agent
docker compose -f ./compose.yaml logs --tail=100 prototype-sandbox-1 prototype-sandbox-2
```

查看或立即更新 snapshots：

```bash
crontab -l | grep work-agent-snapshots
tail -n 50 "${XDG_STATE_HOME:-$HOME/.local/state}/work-agent/snapshots.log"
./scripts/update-snapshots.sh
```

查看或立即執行清理：

```bash
crontab -l | grep -E 'work-agent-(artifact|workspace)-cleanup'
tail -n 50 "${XDG_STATE_HOME:-$HOME/.local/state}/work-agent/artifact-cleanup.log"
tail -n 50 "${XDG_STATE_HOME:-$HOME/.local/state}/work-agent/workspace-cleanup.log"
./scripts/cleanup-artifacts.sh
./scripts/cleanup-workspaces.sh
```

清理腳本自己不刪檔，都在擁有該目錄的 container 內執行：drafts 的規則正本在 agent-config
（`/home/node/code/agent-config/skills/slack-list/scripts/slack-list cleanup`），thread workspace 是
`thread-workspace cleanup`，out 是 `sandbox-runner cleanup-out`。host 端不要直接動 `runtime/drafts`、
`runtime/work`、`runtime/out`。log 裡出現 `FAILED` 就是那一輪沒清到（多半是 container 沒在跑），會留到
下一輪；持續 FAILED 要當成待處理，不是雜訊（告警也會報）。

`update-snapshots.sh` 同步完會用 runtime image重建 CodeGraph索引，所以 host不需要安裝 Node。
索引放在 `$SNAPSHOT_ROOT/.index/<repo>`，每個 snapshot內的 `.codegraph` 是指過去的相對 symlink。
第一次部署時 image還沒 build，那一輪的索引會被跳過並印出訊息，`deploy.sh` 之後再跑一次同步即可。
snapshot 端不要跑 `git gc --prune=now`：thread workspace 是 `git clone --shared`，借用 snapshot 的物件。

停止 agent：

```bash
docker compose -f ./compose.yaml down
```

不要執行 `docker compose -f ./compose.yaml down -v`，它會刪掉 `codex-home`（所有 thread 的 Codex
session，之後 `session/load` 全部接不上）、`pnpm-store` 與兩格 scratch。`runtime/` 不會被 `down` 刪除。

### 升級版本

版本正本只有 `config/versions.env` 一份；改完要同步 `compose.yaml` 的 build args（`tests/static.sh` 會擋）。

- **OpenAB**：取得新的 OpenAB codex image multi-arch digest，更新 `OPENAB_VERSION`、`OPENAB_IMAGE`，先確認新版本
  的 Slack config。
- **codex／codex-acp**：`CODEX_VERSION` 與 `CODEX_ACP_VERSION` 一起升；`config/openab.toml` 的 `CODEX_PATH`
  帶版本，要一起改；從新版 codex 的 `codex-rs/models-manager/models.json` 重產 `config/codex/catalog.json`
  （複製 `gpt-6-sol` 那筆、只改 `slug`）。catalog parse 失敗 codex 整個起不來。Dockerfile 會在 build 時驗
  exact version。
- **sandbox**：`SANDBOX_NODE_IMAGE`（帶 digest）、`PLAYWRIGHT_VERSION`、`OFFICEPARSER_VERSION`；sandbox 的
  image tag 帶 node 與 Playwright 版本，也要一起改。
- **Caddy**：`CADDY_VERSION` 與兩個 SHA-512 一起改，再重跑 `sudo ./scripts/install-host-network.sh install`。

改完 `./tests/static.sh && ./scripts/deploy.sh`。

### 更新或新增 Skill

`/home/node/.agents/skills` 是 `agent-config/skills` 整個目錄的 read-only mount。`agent-config` 是 skill 與
MCP 定義的 canonical public catalog，由 skillshare 管理；更新它只會改變 snapshot 裡可讀到的 catalog，
不會自動新增這個 bot 的 runtime capability。Codex 沒有 skill deny 設定，bot 能用哪幾支只寫在
`agents/CLAUDE.md`「Skill 邊界」；要讓 bot 使用新能力，必須先 review 那份 contract 與 Slack Home，再依需要
部署。不要在 runbook 另列一份可獨立修改的 skill allowlist。skill 怎麼安裝、更新第三方來源，看
agent-config 自己的 README。

要立即生效就在 host上執行 `./scripts/update-snapshots.sh`。

`slack-list` 設定只讀環境變數：container 從 `env/openab.env` 拿到，不需要 `~/.config/slack-list/.env`。

從 work-helper 切過來的 host，snapshot root 裡還留著舊的 `work-helper` 目錄，`preflight.sh` 會因為它不在
`config/repos.conf` 而失敗。確認新的 `agent-config` snapshot 同步成功後，刪掉 `$SNAPSHOT_ROOT/work-helper`
與 `$SNAPSHOT_ROOT/.index/work-helper`。

需要改這個 repo的情況包括：要讓 catalog 中的新能力成為 bot capability、它需要新的 mount，
或它的行為會撞到 Slack 回覆規則。這時要同步 review `agents/CLAUDE.md` 的 contract、mount 與 Slack Home；
不要只同步 catalog，否則 agent 可能在 Slack 上嘗試後失敗。

已啟動的 session可能已把舊skill內容讀進context。同步後使用新的Slack thread或等待session回收再驗證，不用以舊session判斷同步失敗。

### 新增唯讀 Repo

新增repo會擴大workspace agent可讀資料的範圍，必須當成能力與權限變更review。整個 snapshot root是單一 mount，所以只改一個檔案：

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

最後依「驗證安全邊界」確認所有repo snapshot不可寫、drafts與 work 可寫，並從新的Slack thread詢問新repo內容。
