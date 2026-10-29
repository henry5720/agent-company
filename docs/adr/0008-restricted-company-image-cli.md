# 生圖走一支受限 CLI，不讓 agent 自己打 gateway

## Status

Accepted（已決定）。Amends [0007](0007-single-container-opencode-runtime.md)。
Amended by [0012](0012-follow-the-official-openab-codex-deployment.md)（2026-10-29）：runtime 換成 Codex，
決定本身不變；下面提到 OpenCode 與 Claude specialist 的段落是歷史。

## Background

[0006](0006-readonly-product-context-handoff-bot.md) 原本用一個 image-artifact broker 提供生圖，
[0007](0007-single-container-opencode-runtime.md) 把 broker 拿掉，理由是它帶來的隔離有限。

但拿掉之後沒有任何東西補上生圖這件事：

- `config/opencode/opencode.json` 的兩個模型都宣告 `output: ["text"]`，OpenCode 不會從它們拿到圖。
- `oh-my-opencode-slim.json` 的 `image_routing: "direct"` 管的是「Slack 傳進來的附件送到哪個
  agent」，跟產圖無關。
- Claude specialist 與 OpenCode 都依唯一 config 的 `inherit_env` 取得必要的 gateway 設定。

同時 `README.md`、`docs/system-design.md` 與 `agents/CLAUDE.md` 都寫著「圖片產物 PNG」「生圖走
公司 gateway」。也就是說：文件宣告了一個沒有實作的能力，單一 runtime 都是。

補這個洞有兩條路。一條是不補 —— 讓 agent 自己用 `curl` 打 gateway，反正 bash 是 allow、key 也在
env 裡。那等於把 endpoint、header、model、輸出路徑全部交給模型當次自由發揮，key 會出現在
`curl` 指令列與 log 裡，而且沒有任何地方能測「請求長什麼樣」。另一條是給一支窄到只能做這件事的
入口。

## Decision

- 生圖與改圖只有一個入口：`agents/bin/company-image`，由 `Dockerfile` 裝到
  `/usr/local/bin/company-image`，root 擁有、0755。
- 介面刻意窄：
  - mode 只有 `generate` 與 `edit` 兩個固定 subcommand。
  - 參數只有 `--prompt`、`--size`、`--name`，以及 `edit` 的 `--source`。
  - `--size` 只收 `1024x1024`、`1024x1536`、`1536x1024`。
  - `--name` 只收 `[a-z0-9-]`，所以輸出走不出 drafts。
  - `--source` 必須是絕對路徑，resolve 之後必須落在 `/home/node/drafts`、`/home/node/.openab`
    或 `/tmp`，而且開頭必須是 PNG 或 JPEG magic。唯讀 snapshot 不在允許清單裡。
  - 沒有 endpoint、header、model、輸出路徑、重試次數這些參數。
- gateway 位置與 key 只從 runtime env（`COMPANY_GATEWAY_BASE_URL`、`COMPANY_GATEWAY_API_KEY`）
  讀。原始碼裡沒有 URL，也沒有 key；`tests/static.sh` 會掃。
- 請求固定是公司 gateway 的 Responses API `image_generation` tool：`generate` 只送
  `input_text`，`edit` 另外附一張 `input_image` data URL，`tool_choice` 固定指到該 tool，
  `output_format` 固定 `png`。回應只認 `image_generation_call` 的 base64 結果，而且要通過 PNG
  magic 檢查才寫檔。
- 回應的兩種形狀都要讀：一般 JSON body，以及 `Content-Type: text/event-stream` 的 SSE。挑哪一種
  看回應的 Content-Type，不預設 gateway 會回哪一種（SSE body 但沒有那個 header 時，用 body 開頭
  的形狀判斷）。SSE 逐行讀，單行與整份、以及要解碼的 base64 都有大小上限；`partial_image_b64`
  當成一張完整的圖而不是要接起來的 chunk，所以同一個 output 後到的取代先到的，帶 `result` 的收尾
  事件優先於 partial。錯誤只印截斷過的說明，不把整個 event（可能就是那張圖）倒進 stderr。
- 輸出一律寫到 `/home/node/drafts/<UTC 日期>/<name>.png`，檔名衝突時加序號，不覆蓋。
- 選 CLI 不選 MCP server：MCP 要在單一 runtime 各設定一次、各自維護一份 server 定義，而這裡需要
  的東西就是一個帶四個參數的呼叫。CLI 由 image 提供，單一 runtime 不必各自設定就都拿得到。
- 單一 runtime 共用它：`config/openab.toml` 把
  `COMPANY_GATEWAY_*` 放進 `inherit_env`，`config/opencode/opencode.json` 與
  `managed-claude-settings.json` 都明列允許執行它。
- 把 PNG 送回 Slack thread 不在這個 ADR 裡。CLI 只負責產出檔案並印出路徑。

## Consequences

- 生圖從「文件寫了但沒有實作」變成一條可測的路徑：`tests/image-runtime.py` 用假的 `urlopen`
  驗請求 shape、兩種回應形狀、PNG 輸出位置與各種不合法輸入被擋，不需要 Docker、不需要真的 key。
- 只吃 JSON 的版本在真 gateway 上是壞的：實測 `POST {base}/responses` 的 image_generation 請求
  回 `200 text/event-stream`，圖在 `response.image_generation_call.partial_image` 事件的
  top-level `partial_image_b64`（單一 event 就有 ~1.19M base64 字元，`output_format` 是 png），
  所以整個 body 直接 `json.loads` 一定失敗在「gateway response is not JSON」。兩種都接的代價是
  多一個 parser，好處是 gateway 之後改回非 stream 也不會再壞一次。
- 代價是這支 CLI 的介面就是能力上限。要多一個尺寸或多一種 mode 都得改 repo 並重新 build image，
  不能在 Slack 當場繞過。這是刻意的。
- key 仍然在同一個 container 裡，agent 仍然可以自己 `curl`。這支 CLI 不是沙箱，是把「正常路徑」
  做成一個窄入口，並且讓 `agents/CLAUDE.md` 有一個明確的東西可以指。0007 記的取捨沒有改變。
- gateway credential 現在單一 runtime 都需要，`scripts/preflight.sh` 因此不再只在 OpenCode
  runtime 檢查它們。
- CLI 只寫檔、只印路徑。上傳回 Slack thread 是另一條還沒收斂的路徑，在它收斂之前，
  `agents/CLAUDE.md` 要求 bot 照實說「檔案產好了」而不是「已經傳給你了」。
