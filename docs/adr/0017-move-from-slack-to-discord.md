# Slack 關了，bot 改接 Discord；待辦表不搬

## Status

Accepted（2026-11-23）。harness 形狀照 [0012](0012-follow-the-official-openab-codex-deployment.md)，只換聊天平台。

## Background

公司的 Slack workspace 已關，bot 收不到任何訊息。綁在 Slack 上的有四塊：

- OpenAB 的 `[slack]` adapter（Socket Mode、bot token、app token）。
- `slack-thread-artifact`：直接打 Slack 的 `files.getUploadURLExternal`／`files.completeUploadExternal`，把 drafts 傳回 thread。
- agent-config 的 `slack-list` skill：讀寫 Slack List「Bug/需求總表」，`scripts/cleanup-artifacts.sh` 也借它的 `cleanup` 清 drafts。
- Slack Home（`config/slack-home.json`、`scripts/publish-slack-home.sh`）。

釘住的 OpenAB 0.10.0-beta.3 已經內建 Discord adapter（`crates/openab-core/src/discord.rs`），不用升級。

## Decision

1. **`config/openab.toml` 改用 `[discord]`**：`allow_user_messages = "involved"`，頻道裡 @bot 會從那則訊息開 thread，之後在 thread 裡不用再 @。`allow_dm = true`，因為接同事 Chrome、Windows 的網址和 key 只在 DM 交付。
2. **`discord-thread-artifact` 取代 `slack-thread-artifact`**：路徑安全的做法不變。上傳改成一個 multipart `POST /channels/{id}/messages`。目標照 OpenAB 給的 `<sender_context>` 決定：
   - 有 `thread_id`：送到那個 thread。
   - 沒有、而且是 DM：送回 DM。
   - 沒有、而且在頻道：送到 `message_id`。OpenAB 用 `create_thread_from_message` 開 thread，Discord 這種 thread 的 ID 等於那則訊息的 ID。

   DM 與否從 ID 看不出來，Slack 時代「`D` 開頭」的判斷不能用，所以 CLI 多一個 `--check-dm`，問 Discord 的 channel type。單檔上限從 20 MiB 降到 10 MiB，配合沒有 boost 的 server。
3. **待辦表不搬**：Slack List 跟 Slack 一起沒了，bot 先不提供待辦，skillshare 用 `exclude` 不再把 `slack-list` 裝進 bot。之後要待辦，另外決定接哪個 tracker。
4. **drafts 清理改用 GNU `find`**：`find -P -type f -mmin +1440 -delete` 不追 symlink，並用 `unlinkat` 對已開的目錄刪，不需要再依賴 `slack-list cleanup`。
5. **Slack Home 刪除**：Discord 沒有對應的 Home tab。

## Consequences

- container 裡的憑證從 Slack bot token 換成 Discord bot token，取捨跟 0012 一樣：token、gateway key、agent 跑的 code 在同一個 container。
- server 裡每個成員都能私訊 bot，每個 DM 佔一個 session（`[pool] max_sessions`）。
- 「檔案回原 thread」依然只是 OpenAB、agent 與 CLI 之間的信任約定（[0009](0009-thread-artifact-upload-and-optional-stt.md)），不是安全保證。
