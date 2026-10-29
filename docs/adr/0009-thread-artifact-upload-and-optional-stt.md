# ADR 0009：受限 thread artifact uploader 與可選 STT

## 決定

保留單一 OpenAB container，不增加 broker 或 relay；在 runtime image 內加入
`slack-thread-artifact`。它只讀 `/home/node/drafts` 內的 regular PNG、Markdown、HTML，固定以
現有 `SLACK_BOT_TOKEN` 執行 `files.getUploadURLExternal`、upload、`files.completeUploadExternal`，並以
agent 從 OpenAB `<sender_context>` 明確傳入的 `channel_id`、`thread_ts` 回原 thread。成功才刪檔；任一步失敗保留檔案給 host cleanup。

## 兩個 Slack Web API 呼叫用 form body，不用 JSON

原本兩個 method 都送 `Content-Type: application/json` 加 JSON body。實測公司 workspace 的
`files.getUploadURLExternal` 對那個 body 回 `invalid_arguments`，所以 bot 一個檔案都傳不出去；
同一個 PNG 換成 `application/x-www-form-urlencoded` 的 `filename=<name>&length=<bytes>` 就回
`ok: true` 與 `upload_url`／`file_id`。form 是 Slack Web API 的共通表示法，JSON 只有一部分 method
支援，因此兩個 method 都固定走 form。

`files.completeUploadExternal` 的 `files` 是陣列，在 form 裡的表示法是**一個 JSON 字串欄位**
（`files=[{"id":...,"title":...}]` 經 percent-encode），不是 nested JSON body。`channel_id` 與
`thread_ts` 是普通字串欄位。

中間真正上傳 bytes 的那一段**不是** Slack Web API：它只用 Slack 上一個回應給的 upload URL、
artifact 的 mime 當 `Content-Type`、原始 bytes 當 body，而且不帶 bot token。改 Web API 的編碼時
不要順手把它也改成 form。

`slack_form()` 會拒絕非 `str` 的欄位值。這樣「直接把 list／dict 丟進來」會在本機當場失敗，不是
被 urlencode 靜靜轉成 Python repr 再由 Slack 拒絕。header、body 與 file_id 都不進 stderr，錯誤訊息
只有 method 名稱與 Slack 的 error code。

## 「回原 thread」是什麼等級的保證

明確記下來，避免之後被讀成安全機制：

OpenAB 不提供 ACP thread metadata side-channel。`sender_context` 是 **prompt content**，不是不可竄改的
授權資料。整條路徑是「OpenAB 把 context 寫進 prompt → agent 照 `agents/AGENTS.md` 的規則逐字帶入
→ CLI 只驗格式」，三段都在同一個 container、共用同一個 `SLACK_BOT_TOKEN`。可被信任的理由是三段
同屬一個信任域，不是任一段驗證了另一段。

因此這是**單一 container 的信任約定，不是安全保證**。不宣稱 token 隔離、cryptographic binding，也
不宣稱防 prompt injection：能改變 agent 送出什麼值的輸入，就能改變檔案送到哪個 thread（限於這個
bot token 本來就進得去的 conversation）。CLI 的價值只在移除任意 URL、header、命令、檔案路徑與
輸出位置的自由度，並把來源鎖在 drafts。

刻意不改成 broker 或 relay。那需要另一個 process 持有 token 並自行判斷 thread 歸屬，而它能依據的
仍然只有 OpenAB 給的同一份 context —— 換不到新的保證，只多一層要維護的東西，也和
[0007](0007-single-container-opencode-runtime.md) 的單 container 決定衝突。

## 路徑安全

CLI 的檔案處理不用 `resolve()`：resolve 跟著 symlink 走，檢查過的字串和真正開到的檔案可以是兩回事。
固定從 `/home/node/drafts` 開一個 dirfd，逐段 `O_DIRECTORY|O_NOFOLLOW` 往下走；讀取只從那個 fd，
刪除只用同一個 parent fd 並先比對檔案 identity（dev、inode、mode、size、mtime、ctime）。`company-image`
的輸出同樣以 dirfd 握住日期目錄跨過 gateway 呼叫，檔名用 `O_CREAT|O_EXCL|O_NOFOLLOW` 佔住，內容
先寫暫存檔再 fsync、rename。父目錄或檔案在檢查之後被換掉的結果是失敗，不是逃出 drafts。攻擊佈局
由 `tests/artifact-path-safety.py` 重現。

單一 agent runtime 允許同一支命令；Claude ACP 只作 OMO specialist。

OpenAB 兩份 config 都放可選 `[stt]`：`STT_BASE_URL` / `STT_API_KEY` 供標準
`/audio/transcriptions` 使用，預設 `enabled = false`。未確認相容 STT provider 前，audio 直接拒絕；不把
company gateway 當成必定提供 STT 的服務。

## 後果

- 同 container 的 Slack token 同時可供 OpenAB、`slack-list` 和 uploader 使用，這是已接受的界線。
- 上傳成功後草稿不可恢復；失敗時不刪，讓 cleanup 保留可觀察與人工處理的機會。
- 「失敗保留 24 小時」需要有東西真的去刪：deployment host 的 `# agent-company-artifact-cleanup`
  crontab entry 每小時 :17 執行 `scripts/cleanup-artifacts.sh`，它在 container 內跑
  `slack-list cleanup`。刪除規則的正本在 work-helper，部署層只負責排程與記錄失敗。因為檢查
  每小時一次，實際壽命是 24 到 25 小時，不是剛好 24。
- 上傳成功但草稿在讀取後被換掉時，CLI 不刪那個檔案、在 stderr 說明並仍 exit 0：交付確實發生了，
  回非零只會讓 agent 重傳。殘檔交給上面那個 cleanup。
- 真實 Slack upload 和真實 STT 仍需在有 runtime secret 的 container 做 smoke test；離線測試只驗
  request shape、路徑安全與失敗語意。這些列在 `docs/runbook.md` 的「人工 release gate」，
  `scripts/deploy.sh` 不會、也不聲稱會自動驗到。
- `invalid_arguments` 那次是離線測試驗到「打對 endpoint」但沒驗 body 編碼漏掉的。
  `tests/slack-thread-artifact.py` 現在逐項比對三個請求的 method、URL、header 與解回來的
  body；`tests/static.sh` 另外擋「改回 JSON body」。兩者都仍然不證明 Slack 收不收。
