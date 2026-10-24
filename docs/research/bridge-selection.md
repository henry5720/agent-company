# 選型：Slack → coding agent 的 bridge（issue #3）

調查日期：2026-10-24。所有結論以原始碼為準：

| 專案 | commit | 本機路徑 |
|---|---|---|
| openabdev/openab | `20830d5d`（2026-10-23） | `~/.cache/wayfinder/src/openab` |
| chenhg5/cc-connect | `d01af45c`（2026-10-23） | `~/.cache/wayfinder/src/cc-connect` |

## 結論

**留 OpenAB，後端從 `opencode acp` 換成 `codex-acp`（之後 `claude-agent-acp`），不換 bridge。**
OpenAB 有官方 immutable digest image、agent process `env_clear()`、每 thread 一個 process，換 agent 只改 `[agent].command` 與 image variant；
不選 cc-connect，因為 repo 沒有 LICENSE 檔（GitHub API 回 404，只有 README 宣稱 MIT）、沒有官方 container image，而且 agent 子行程預設繼承 bridge 的全部 env。

兩者共同缺口：都沒有「每個 thread 自動一個工作目錄」。

## 逐項比較

| # | 項目 | OpenAB @ 20830d5 | cc-connect @ d01af45 |
|---|---|---|---|
| 1a | thread 對話 | 有。turn key 用 `thread_ts`（`crates/openab-core/src/slack.rs:1038-1043`），session pool 以 thread_key 索引（`crates/openab-core/src/acp/pool.rs:20`） | 有，但要設 `session_scope = "thread"`；預設是 `user`（`platform/slack/slack.go:89-137`，key 為 `slack:<channel>:t:<threadTS>`） |
| 1b | 收圖片／檔案 | 有。讀 `event["files"]`（`slack.rs:1488`），圖片 `download_and_encode_image`（`slack.rs:1623`），文字檔 5 個／1 MB 上限，大檔走 filestore（S3/R2） | 有。`processSlackFileShares`（`platform/slack/slack.go:407-462`）分圖片／音訊／一般檔；一般檔寫到 `<work_dir>/.cc-connect/attachments`（`core/message.go:157`、`agent/codex/session.go:143`）→ **work_dir 必須可寫**，與本 repo `/home/node/code` 唯讀衝突 |
| 1c | 回傳檔案 | **沒有**。整個 repo 找不到 `files.getUploadURLExternal`／`files.upload`；本 repo 自寫 `agents/bin/slack-thread-artifact` 補上 | 有。`SendImage`／`SendFile` 用 `UploadFileV2` 回原 thread（`slack.go:544-610`）；agent 用 `cc-connect send --file`（`cmd/cc-connect/send.go:119`）經 unix socket 觸發，靠 env `CC_SESSION_KEY`（`core/engine.go:4385`）定位 session；可用 `attachment_send = "off"` 關掉 |
| 1d | DM 與 channel | 都有。`is_dm = channel_id.starts_with('D')`（`slack.rs:1000`、`1379`）；channel 需 mention 或已參與 thread | 都有。channel 走 `AppMentionEvent`（`slack.go:206`），DM 走 message.im；DM 頂層訊息不開 thread，回落 user-scope session（`slack.go:305-310`、`487-497`） |
| 2 | Codex 接自訂 Responses endpoint | 不擋。agent 只拿到 `HOME/PATH/USER` + `[agent].env` + `inherit_env`（`acp/connection.rs:393-420`、`206`）；把 `~/.codex/config.toml` 的 `[model_providers.x] base_url/wire_api="responses"` 單檔掛進去、key 用 `inherit_env` 傳即可。官方範例 `config.toml.example:202-207` | 內建。provider 設定會寫 `[model_providers.<name>]` 含 `base_url`、`wire_api`、`http_headers`（`agent/codex/provider_config.go:15-100`），也可 `-c openai_base_url=`（`agent/codex/appserver_session.go:276-278`）；或 `[projects.agent.options.env]` 傳 env（`agent/codex/codex.go:80-95`） |
| 3 | 換到 Claude Code 成本 | 改設定 + 換 image variant：`command = "claude-agent-acp"`（`config.toml.example:182-187`），官方有 `Dockerfile.claude`／`-claude` tag。本 repo 已裝固定版 claude-agent-acp 與 Claude Code CLI | 改設定：`[projects.agent] type = "claudecode"`（`config.example.toml:855-860`），同一個 binary |
| 4a | 每 thread 一個 process | 是（ACP 長駐 process，pool 以 thread_key 管，`max_sessions` 上限） | session 依 session key 分。Codex 預設 `exec` backend 是**每個 turn 起一次** `codex exec`（resume 接續，`agent/codex/session.go:168`）；`backend = "app_server"` 才是每 session 一個長駐 process（`agent/codex/codex.go:532`） |
| 4b | 工作目錄依 thread 自動分開 | 否。`per_thread_workdir` 只出現在 `DESIGN.md:117`，code 沒有 | 否（Slack）。multi-workspace 是**依 channel 名稱**對到 `base_dir/<channel>`（`core/engine.go:17460-17495`）；只有 Feishu 實作 `ChannelScopedSessionKey` 能依 thread 分（`platform/feishu/feishu.go:4641`），Slack 沒實作 |
| 5 | bot token 會不會傳給 agent | 預設不會：`cmd.env_clear()`（`acp/connection.rs:395`），列進 `inherit_env` 才會傳且會印警告（`connection.rs:448`）。**注意本 repo `config/openab.toml` 已把 `SLACK_BOT_TOKEN` 放進 `inherit_env`**（為了 slack-list／artifact 上傳），所以目前實際上 agent 拿得到 | 預設會：`cmd.Env = core.MergeEnv(os.Environ(), …)`（`agent/codex/session.go:172`、`appserver_session.go:291`），bridge 的 env 全部繼承。只有設 `run_as_user`（sudo 換 uid）才走 allowlist（`core/runas.go:47-53`、`185`）；且 token 通常寫在 agent 同 uid 可讀的 `config.toml` |
| 6 | Docker 單 container + 固定版本 | 有。`ghcr.io/openabdev/openab:<ver>-<agent>` multi-arch，例：`0.10.0-beta.6-codex@sha256:6cb975d4…`、`0.10.0-beta.6-claude@sha256:da3f80f5…`（`docker buildx imagetools inspect` 實查）。官方 k8s 是 replicas:1 單 container | 無官方 image／Dockerfile（repo 內找不到）。只有 GitHub release binary + `checksums.txt`（v1.5.0），要自己寫 Dockerfile 用 sha256 釘版 |
| 7 | 活躍度、授權 | MIT（`LICENSE`）；827 stars；近 3 個月 44 commits、11 位作者；最新 `openab-0.10.0-beta.6`（2026-10-23），版號仍是 beta | **無 LICENSE 檔**（`gh api repos/chenhg5/cc-connect/license` → 404），README 寫 MIT；16k stars；近 3 個月 199 commits、75 位作者；最新正式版 v1.5.0（2026-08-16），之後只有 beta |

## 簡短排除

- **openclaw/openclaw**：完整的個人 AI agent 平台（自帶 agent loop、記憶、多平台），不是把 Codex／Claude Code 接上 Slack 的薄 bridge。
- **NousResearch/hermes-agent**：同上，是自己的 agent runtime，後端不是 Codex CLI。
- **Claude in Slack／Codex Slack 整合**：原廠雲端執行，不能指向公司自訂 Responses endpoint，也不能跑在自家 Docker 內讀 read-only snapshot。
- **其他小型 bridge**（`usuginus/slack-codex-bridge` 8 stars、`mpociot/claude-code-slack-bot` 無授權、最後 push 2025-06）：單一 agent、維護量太小，未深讀原始碼。

## 換 Codex 時要處理的事（OpenAB）

1. `[agent] command = "codex-acp"`；用 `-codex` image 或在本 repo Dockerfile 固定版本安裝 `@agentclientprotocol/codex-acp` 與 `@openai/codex`（官方 `Dockerfile.codex:32` 的做法）。
2. Codex 的 `config.toml` 單檔唯讀 mount，`CODEX_HOME` 本身可寫（與 `OPENCODE_CONFIG_DIR` 同樣理由）。
3. `default_config_options = { mode = "agent-full-access" }`（`config.toml.example:205-207`）。
4. 重新決定 `SLACK_BOT_TOKEN` 是否留在 `inherit_env`；這是目前唯一讓 env_clear 失效的地方。

## 沒查到／未驗證

- 沒實際啟動任何一個 bridge 打 Slack；所有 Slack 行為只讀碼。
- 沒驗證 `codex-acp` 在 OpenAB 下打公司 gateway `/responses` 真的通（只確認 bridge 不擋、Codex 支援 `wire_api`）。
- cc-connect 在 Docker 內的行為、`run_as_user` 在 container 內是否可用，未測。
- cc-connect 授權的實際狀態（是否在其他地方有授權聲明）未向作者確認。
- 其他小型 bridge 未讀原始碼。
