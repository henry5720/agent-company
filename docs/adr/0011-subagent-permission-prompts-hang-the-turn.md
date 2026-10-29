# subagent 的權限詢問沒人回答，所以 external_directory 設成 allow

## Status

Superseded（2026-10-29）：OpenCode 已退場，Superseded by [0012](0012-follow-the-official-openab-codex-deployment.md)。
原本：Accepted（已決定）。補 [0007](0007-single-container-opencode-runtime.md) 的
permission 設定。

## Background

Slack 上一個 thread 完全沒收到回覆。Container 是健康的，`opencode acp` 沒有 crash，
也不是 [0010](0010-company-provider-uses-the-responses-api.md) 那種 405。

2026-09-11 那一輪的軌跡（時間都是 UTC，取自 container log 與
`/home/node/.local/share/opencode/log/opencode.log`）：

```
01:49:06  session resumed via session/load   thread slack:1788763243.776879
01:56:04  loop step=0，orchestrator 派出 fixer ×2、designer 三個 subagent
01:57:55  asking permission=external_directory patterns=["/home/node/drafts/*"]
01:58:01  asking permission=external_directory patterns=["/home/node/drafts/*"]
01:58:38  asking permission=external_directory patterns=["/home/node/drafts/*"]
          ← 這三筆在 container log 裡沒有對應的 auto-respond，之後靜止 27 分鐘
02:26:04  message=cancel（使用者在同一個 thread 又貼了一則，OpenAB 取消整輪）
```

OpenAB 本來就會自動回答 ACP 的權限詢問：

```
auto-respond permission title="/home/node" outcome={"optionId":"always",...}
```

08-25 到 09-09 之間的 12 筆 `asking` 全部在 10ms 內出現對應的 `auto-respond`。那 12 筆
發的時候跑的都是 orchestrator（`gpt-5.6-terra`，主 session）。卡住的三筆發的時候跑的是
fixer／designer subagent（`gpt-5.6-luna`，log 標 `managesSession:false`）。02:27:48 主
session 自己問同一個 `/home/node/drafts` 時，auto-respond 照樣秒回。

也就是說：**OpenAB 只自動回答主 session 的權限詢問，subagent 的沒人回答。** 沒人回答的
那一輪不會結束，OpenAB 不會發回覆，PM 看到的就是 bot 裝死；要等到有人再貼一則訊息把它
cancel 掉。

`external_directory` 是唯一會走到「詢問」的權限：`read`／`glob`／`grep`／`list` 預設
allow，`edit` 是 deny（直接拒絕，不問），`bash` 有明確規則。而 OMO 的草稿正本
`/home/node/drafts` 在 working dir `/home/node/code` 之外，所以只要 subagent 用檔案
工具碰草稿就會觸發。

## Decision

`config/opencode/opencode.json` 的 `permission.external_directory` 設成 `"allow"`。

理由不是「這個權限不重要」，是**它本來就擋不住任何東西**：同一份設定裡
`bash` 是 `"*": "allow"`，agent 隨時可以 `cat`、`python3` 讀寫同一批路徑（今天那一輪
的兩份草稿就是這樣寫出來的）。真正的邊界在 mount：snapshot root 唯讀，只有
`/home/node/drafts` 與 `/home/node/code/.index` 可寫，`config/opencode/` 每個檔都是單檔
唯讀 mount。留著 `ask` 換到的不是安全，是 bot 對 PM 靜默。

不做逐路徑 allowlist（`/home/node/drafts*`、`/home/node/.claude/skills*` …）：pattern
的比對語意沒有文件可查，猜錯就退回 `ask`，也就是退回這個 bug 本身；而且它一樣擋不住
bash，等於用複雜度換零收益。

不動 OpenAB 的 auto-respond 行為。那是上游 binary，不在這個 repo 的正本範圍內。

## Consequences

- OMO 委派 subagent 寫草稿不再卡住整輪。
- `tests/static.sh` 釘住 `external_directory == "allow"`，改回 `ask` 會被擋下來並指回
  這份 ADR。`edit == "deny"` 的檢查不變。
- 這份設定是單檔唯讀 mount，改完要重啟 container 才會重讀。
- **靜態測試證明不了這件事修好了。** 它只證明設定值是什麼。真正的驗法是在 Slack 讓
  bot 跑一輪會派 subagent 寫草稿的工作，然後確認 container log 有 `auto-respond` 或
  根本不再出現 `asking permission=external_directory` —— 屬於
  `docs/runbook.md`「人工 release gate」。
- 如果哪天 OpenAB 補上 subagent 的 auto-respond，這一條可以拿掉，但沒有必要拿掉。
