# 新架構需要的最小 Slack scope（issue #6）

調查日期：2026-10-24。結論以原始碼與 Slack 官方文件為準：

| 來源 | 版本 | 用途 |
|---|---|---|
| openabdev/openab | `20830d5d`（2026-10-23，前一張 research 選定的 bridge） | OpenAB 自己打的 Slack API 與收的 event |
| henry5720/agent-config | `76deccff`（2026-10-20） | `skills/slack-list/scripts/slack-list` |
| 本 repo | `066557b`（main） | `agents/bin/slack-thread-artifact`、`scripts/publish-slack-home.sh`、`config/openab.toml` |
| Slack 官方 | docs.slack.dev（2026-10-24 抓取） | 每個 method／event 要求的 scope |

前提：後端換成 `codex-acp` 不改變 Slack 用量 —— Slack 只由 OpenAB 與上面三支 CLI／script 呼叫。
新架構的 agent 在 container 內 full access，又拿得到 `SLACK_BOT_TOKEN`（`config/openab.toml:28` 的
`inherit_env`），所以 **token 帶的每個 scope 都等於 agent 能做的事**，下表就是外流時的損害上限。

## 結論

**Bot token 留 10 個 scope、bot events 留 3 個；`groups:read` 與所有 runbook 沒列的 scope 都移除。
移除要先 revoke／uninstall 再裝，單純 Reinstall 降不了 scope，token 一定換新，bot 會被移出所有 channel。**

保留的 bot token scopes：

```text
app_mentions:read  chat:write  files:read  files:write  groups:history
im:history  lists:read  lists:write  reactions:write  users:read
```

保留的 bot events：`app_mention`、`message.groups`、`message.im`（與 `docs/runbook.md:23` 相同）。
App-level token 維持只有 `connections:write`（Socket Mode，`docs/runbook.md:22`）。

## 每個 scope 誰用到

method 要求的 scope 一律取自 `https://docs.slack.dev/reference/methods/<method>` 的 Facts 欄。

| Scope | 呼叫者（檔案:行號） | 官方要求來源 | 決定 |
|---|---|---|---|
| `app_mentions:read` | OpenAB 處理 `app_mention` event（openab `crates/openab-core/src/slack.rs:935`） | [app_mention](https://docs.slack.dev/reference/events/app_mention) Required Scopes | 留 |
| `chat:write` | OpenAB `chat.postMessage`／`chat.update`（`slack.rs:446`、`530`）；slack-list 在 item 留言串發註記 `chat.postMessage`（`slack-list:677`、`1510`、`1535`） | [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage)、[chat.update](https://docs.slack.dev/reference/methods/chat.update) | 留 |
| `files:read` | OpenAB 用 bot token 下載使用者附件 `url_private_download`（`slack.rs:1505-1513`，圖片、音訊／STT、filestore 文件都走這裡）；slack-list `files.info` 讀 List schema（`slack-list:411`） | [files.info](https://docs.slack.dev/reference/methods/files.info)；[files:read](https://docs.slack.dev/reference/scopes/files.read)「retrieve … as well as download those files」 | 留 |
| `files:write` | `agents/bin/slack-thread-artifact:279-296`（`files.getUploadURLExternal` → `files.completeUploadExternal`）；slack-list `upload_md`／`upload_bytes`（`slack-list:723-731`、`940-951`） | [files.getUploadURLExternal](https://docs.slack.dev/reference/methods/files.getUploadURLExternal)、[files.completeUploadExternal](https://docs.slack.dev/reference/methods/files.completeUploadExternal) | 留 |
| `groups:history` | `message.groups` event；OpenAB `allow_user_messages = "involved"`（`config/openab.toml:10`）時用 `conversations.replies` 判斷是否參與過 thread（`slack.rs:358-368`、`1165-1172`）；slack-list `conversations.history`／`conversations.replies` 讀 item 留言串（`slack-list:515`、`1707`） | [message.groups](https://docs.slack.dev/reference/events/message.groups)、[conversations.replies](https://docs.slack.dev/reference/methods/conversations.replies)、[conversations.history](https://docs.slack.dev/reference/methods/conversations.history) | 留（見「沒驗到」第 1 點） |
| `im:history` | `message.im` event（DM 入口；OpenAB 以 `channel_id` 開頭 `D` 判斷 DM，`slack.rs:1000`、`1157-1158`） | [message.im](https://docs.slack.dev/reference/events/message.im) | 留 |
| `lists:read` | slack-list `slackLists.items.list`／`items.info`（`slack-list:240`、`433`） | [slackLists.items.list](https://docs.slack.dev/reference/methods/slackLists.items.list)、[items.info](https://docs.slack.dev/reference/methods/slackLists.items.info) | 留 |
| `lists:write` | slack-list `add` 的 `slackLists.items.create`／`items.update`（`slack-list:1367`、`1404`） | [slackLists.items.create](https://docs.slack.dev/reference/methods/slackLists.items.create)、[items.update](https://docs.slack.dev/reference/methods/slackLists.items.update) | 留 |
| `reactions:write` | OpenAB `reactions.add`／`remove`（`slack.rs:494`、`513`），因為 `[reactions] enabled = true` 且 `assistant_mode = false`（`config/openab.toml:11`、`62-63`） | [reactions.add](https://docs.slack.dev/reference/methods/reactions.add) | 留（損害只是加表情） |
| `users:read` | `scripts/publish-slack-home.sh:44` 的 `users.list`（App Home 發佈必要）；OpenAB `users.info` 解析顯示名稱（`slack.rs:249`，失敗退回 user ID，`slack.rs:1719-1722`）；slack-list `users`（`slack-list:1206`，`agents/CLAUDE.md:88` 已處理 `missing_scope`） | [users.list](https://docs.slack.dev/reference/methods/users.list)、[users.info](https://docs.slack.dev/reference/methods/users.info) | 留（Home 發佈沒它不行） |

不需要 scope 的呼叫：`auth.test`（OpenAB `slack.rs:184`、slack-list `slack-list:565`）、
`chat.getPermalink`（`slack-list:620`）、`views.publish`（`publish-slack-home.sh:72`）——三者官方 Facts 都寫
「No scopes required」。App Home 的 Messages Tab 是 app 設定（`docs/runbook.md:24`），不是 scope。

本設定下 OpenAB **不會**打的：`assistant.threads.setStatus`、`chat.startStream`（只在 `assistant_mode` 與
`streaming` 為 true 時，`slack.rs:544-560`；本 repo 兩者都是 false）→ 不需要 `assistant:write`。
`bots.info`（`slack.rs:289`）只在 `trusted_bot_ids` 非空時呼叫，`allow_bot_messages = "off"` 用不到。
OpenAB 沒有任何上傳檔案的程式碼（前一張 research 1c），所以 `files:write` 只為本 repo 的兩支 CLI。

STT：OpenAB 只用同一個 `files:read` 下載音訊（`slack.rs:1519-1526`），轉錄打的是 `STT_BASE_URL`，
不需要額外 Slack scope；目前 `[stt] enabled = false`（`config/openab.toml:39`）。

## 要移除的

| Scope | 為什麼不要 | 官方定義 |
|---|---|---|
| `chat:write.public` | 讓 app 不必被邀請就能貼文到**任何公開 channel**，直接打破「靠邀請維持 channel 邊界」（`docs/system-design.md:65`）。上面所有呼叫者都只回原本的 thread 或 List 留言串 | [chat.postMessage「Channel membership」](https://docs.slack.dev/reference/methods/chat.postMessage)、[chat:write.public](https://docs.slack.dev/reference/scopes/chat.write.public) |
| `channels:manage` | 可 `conversations.create／archive／rename／kick／leave` 公開 channel；沒有任何呼叫者 | [channels:manage](https://docs.slack.dev/reference/scopes/channels.manage) |
| `groups:write.invites` | 可 `conversations.invite` 把人拉進私有 channel；沒有任何呼叫者 | [groups:write.invites](https://docs.slack.dev/reference/scopes/groups.write.invites) |
| `groups:read` | `docs/runbook.md:33` 列了，但三個呼叫者都沒打 `conversations.info／list／members／open`、`users.conversations`（這是它涵蓋的全部 method）。OpenAB `docs/slack.md` 建議表寫「List private channels」，但 `slack.rs` 沒有對應呼叫。agent-config `skills/slack-list/SKILL.md:353-354` 也寫了 `groups:read`，是文件與程式碼不一致 | [groups:read](https://docs.slack.dev/reference/scopes/groups.read) |
| 現行 token 其他 runbook 沒列的 scope | 不在上表＝沒有呼叫者 | — |

**現行 token 到底有哪些 scope 沒有查**：沒有 secret、也刻意不打 Slack API。上面只知道
`docs/system-design.md:65` 記錄的三個加「等」。換 token 前在 deployment host 對舊 token 打一次
`auth.test`，回應的 `x-oauth-scopes` header 就是完整清單（[installing-with-oauth](https://docs.slack.dev/authentication/installing-with-oauth)）。

**不要加回來的**：`message.channels`／`channels:history`（目前兩個 channel 由 `message.groups` 處理，見下面第 2 點）、
`assistant:write`、`im:write`、`users:read.email`。

## 重新安裝的影響

1. **單純 Reinstall 拿不掉 scope。** 官方：「It is not possible to downgrade an access token's scopes」「There is no way
   to remove scopes from an existing token without revoking it entirely」
   （[installing-with-oauth](https://docs.slack.dev/authentication/installing-with-oauth)）。所以 App 設定裡刪掉 scope 後，
   要先 revoke 舊 bot token（[`auth.revoke`](https://docs.slack.dev/reference/methods/auth.revoke)）或整個 uninstall
   （[`apps.uninstall`](https://docs.slack.dev/reference/methods/apps.uninstall)），再 Install。
   `docs/system-design.md:65`「移除多餘 scope 並重新安裝」要補上這一步，否則縮權不會生效。
2. **Token 一定換新。** revoke 後要重新走安裝才拿得到新 `xoxb-…`（auth.revoke：「reset the token value — you can obtain a
   token again with another jaunt through the OAuth process」）。要更新 `env/openab.env`，並重啟 container；host 上
   `publish-slack-home.sh` 用同一個 `SLACK_BOT_TOKEN`（`scripts/publish-slack-home.sh:15`）。
3. **Bot 會被移出所有 channel。** auth.revoke：「deactivate the bot user and remove its channel memberships」。要把 app
   重新邀進 `C0BPZRN6H3R` 與 `C0B9PSESQ2U`（`docs/runbook.md:42`），而且只邀這兩個。
4. **Admin 核准看 workspace 設定。** 開了 app approval 的 workspace，需要 review 的安裝請求預設送給 workspace owner
   （[Manage app approval](https://slack.com/help/articles/222386767-Manage-app-approval-for-your-workspace)）；該文**沒說**
   重裝／改 scope 是否要再核准。本 workspace 有沒有開 approval 沒查。
5. 使用者那邊：DM 不用重做；channel 的既有 thread 在 bot 重新加入後才收得到 `message.groups`。

## 沒驗到／不確定

1. **List item 留言串所在 channel 的類型。** slack-list 把 list ID 的 `F` 換成 `C` 當留言串 channel（`slack-list:158-164`，
   註解寫「沒有官方文件」）。讀它要 `groups:history` 還是 `channels:history`、bot 不在那個 channel 時 `chat.postMessage`
   是否依賴 `chat:write.public`，都沒有官方文件可查。現行 token 有一堆多餘 scope，所以「現在能動」證明不了最小集合
   夠用。換 token 後必須人工跑：`slack-list context`、`slack-list add`（會發留言串註記）、在留言串 @bot 一次。
   App Home 也宣稱「待辦列的留言串」是入口（`config/slack-home.json:79`），那裡的 event 是不是以 `message.groups`
   送達同樣沒驗。若失敗，加回的應該是對應的 `*:history`，不是 `chat:write.public`。
2. **兩個 channel 是不是私有。** runbook 只訂 `message.groups`（`docs/runbook.md:23`），暗示兩者都是私有 channel；
   ID 開頭都是 `C`，看不出來。若有一個是公開的，要加 `message.channels` 與 `channels:history`（openab `docs/slack.md` 第 3、4 節）。
3. Revoke 後 Slack List 對 app 的分享、已發佈的 App Home view、app-level token（`xapp-…`）是否保留，官方文件沒寫；
   保守做法是重裝後重跑 `./scripts/publish-slack-home.sh`、確認 List 仍分享給 app、`openab` 能連上 Socket Mode。
4. OpenAB 版本：本調查讀 `20830d5`；`config/versions.env:11` 目前釘 `0.10.0-beta.3`，換 `codex-acp` 時升到的版本要
   再 grep 一次 `slack.rs` 的 method 清單。
5. 沒有打任何 Slack API、沒讀 `env/openab.env`；所有行為只讀碼與官方文件。真的 Slack 往返屬於
   `docs/runbook.md`「人工 release gate」。
