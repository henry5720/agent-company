# 檔案照 OpenAB 官方寫法用 curl 傳回 Discord，拿掉 discord-thread-artifact

## Status

Accepted（2026-11-23）。Supersedes [0009](0009-thread-artifact-upload-and-optional-stt.md) 的上傳部分（STT 部分不變）。
修訂 [0008](0008-restricted-company-image-cli.md) 的理由，決定不變。

## Background

OpenAB 只把文字串流回 Discord，不轉檔。官方對這件事的建議是 agent 自己打 Discord API：
`POST /channels/{thread_id 或 channel_id}/messages`，multipart 帶 `files[0]`，token 從 agent 環境讀
（OpenAB [docs/codex.md〈Sending Generated Images Back to Discord〉](https://github.com/openabdev/openab/blob/v0.10.0-beta.3/docs/codex.md#sending-generated-images-back-to-discord)，
釘住的 v0.10.0-beta.3）。

0009 在這條路外面包了一支 `discord-thread-artifact`：只讀 drafts、逐段擋 symlink、成功才刪檔。
0009 自己也寫了這不是安全保證：token 在 agent 環境裡、container 是 agent-full-access，agent 本來就能繞過它。
留下的是一支要維護的 CLI、兩支測試，以及每次上傳都要背的三個參數。

## Decision

- 刪掉 `agents/bin/discord-thread-artifact` 與 `tests/discord-thread-artifact.py`；`tests/artifact-path-safety.py` 只留 `company-image` 的部分。
- 傳檔照官方寫法，指令寫在 `agents/AGENTS.md`〈傳回 thread〉。目標規則不變（thread → DM → 頻道第一則的 `message_id`），
  寫在同一份的〈回覆目標〉，瀏覽器的 isolated context 也用它命名。
- 判斷 DM 改成直接 `GET /channels/{id}` 看 `type`（1 = DM），取代 `--check-dm`。
- `DISCORD_BOT_TOKEN` 照舊放在 `[agent].inherit_env`。
- 上傳不刪檔，drafts 一律由 `scripts/cleanup-artifacts.sh` 24 小時後收。

## Consequences

- 少了 drafts 根目錄、symlink、檔案類型的檢查。這些本來就攔不住拿得到 token 的 agent，只在正常操作下少一點副作用。
- 沒有離線測試證明檔案送對地方；靠人工 release gate 第 2 項實傳。
- 上傳時 agent 會把 curl 指令顯示在 thread 裡（OpenAB 顯示工具呼叫的方式）。token 是 `$DISCORD_BOT_TOKEN` 參照，不會以明文出現。

## 0008 的理由更新

`company-image` 保留。0008 當時的理由是 OpenCode 的模型只能輸出文字；現在 runtime 是 Codex，理由換成：
Codex 內建生圖只在 provider 是 OpenAI、`requires_openai_auth` 或帶 OpenAI actor header 時才給模型
（codex `rust-v0.166.0` 的 `codex-rs/ext/image-generation/src/extension.rs`），公司 gateway 這個自訂 provider 三者都不是。
2026-11-23 在 Discord 實測：要求 bot 用內建 `image_gen`，它回「這個環境沒有提供可呼叫的 Codex `image_gen` 工具」；
同一天 `company-image` 透過同一個 gateway 產圖成功。
