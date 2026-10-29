# company provider 走 Responses API，不是 chat completions

## Status

**Superseded by [0013](0013-codex-reaches-the-gateway-through-a-host-proxy.md)。** 下面描述的 OpenCode provider 設定與測試都已退場；「gateway 只開 Responses API」由 0013 重寫。

Accepted（已決定）。Amends [0007](0007-single-container-opencode-runtime.md)、
補 [0008](0008-restricted-company-image-cli.md) 沒寫到的另一半。

## Background

Production 上 bot 完全問不動。在 deployment host 跑最小推論：

```
opencode run --pure --model company/gpt-5.6-terra ...
```

回 HTTP 405（Method Not Allowed）。405 不是 auth 失敗、不是模型不存在、也不是 rate
limit —— 是那個 URL 存在但不收這個 method，換句話說路徑打錯了。

對照本機一個能正常工作的 provider，差別只有一個欄位：

| | 本機（可用） | deployment（405） |
|---|---|---|
| `npm` | `@ai-sdk/openai` | `@ai-sdk/openai-compatible` |

OpenCode 依 provider 設定的 `npm` 欄位決定要呼叫 SDK 的哪個 factory，factory 決定
請求打到 `baseURL` 底下的哪個路徑：

- `@ai-sdk/openai` 建出來的 instance 有 `responses` factory，OpenCode 優先用它 ——
  請求打到 `{baseURL}/responses`。
- `@ai-sdk/openai-compatible` 建出來的 instance **沒有** `responses`，也沒有
  `chat`，只有 `languageModel`／`chatModel`／`completionModel`。OpenCode 只能退到
  `languageModel`，請求打到 `{baseURL}/chat/completions`。

公司 gateway 只提供 Responses API。這件事 repo 裡本來就有兩處寫著：

- [0008](0008-restricted-company-image-cli.md) 明寫「請求固定是公司 gateway 的
  Responses API `image_generation` tool」。
- `agents/bin/company-image` 的 `gateway_endpoint()` 一直是
  `base.rstrip("/") + "/responses"`。

所以 `/chat/completions` 在那個 gateway 上根本不存在對應的 method —— 405。生圖之所以
沒壞，就是因為它走 `company-image`，從來沒有經過這個 provider 設定。

## Decision

- `config/opencode/opencode.json` 的 `provider.company.npm` 固定 `@ai-sdk/openai`。
- `options` 不動。`createOpenAI` 與 `createOpenAICompatible` 的 `baseURL`、`apiKey`
  同名同語意，所以換 adapter 不需要改 env、不需要改 secret 引用方式，兩個值仍然是
  `{env:COMPANY_GATEWAY_BASE_URL}`／`{env:COMPANY_GATEWAY_API_KEY}`。
- model ID 不動，仍然是 `gpt-5.6-terra` 與 `gpt-5.6-luna`。405 是路徑問題，不是模型
  問題；順手改模型名只會讓下一次排障多一個變數。
- 不加 `reasoning`、`tool_call`、`attachment`、`limit` 這些 model metadata。OpenCode
  的設定 schema 把它們全部列為 optional，`attachment` 在沒填時從
  `modalities.input` 推導，而現有設定已經宣告 `["text", "image"]`。這次只修已診斷的
  那一個欄位。
- `COMPANY_GATEWAY_BASE_URL` 的語意寫清楚：它是 gateway 提供 `/responses` 的**前綴**，
  不保證是 `/v1`。現在有兩個消費者接同一個後綴（provider 與 `company-image`），這個
  值必須同時滿足兩邊。`env/openab.env.example` 照這個語意寫。

## Consequences

- `tests/provider-route.py` 把「adapter → factory → URL 後綴」這張表寫下來並跑一次，
  同時驗 provider 與 `company-image` 對同一個 base URL 接出一樣的後綴。把 `npm` 改回
  `openai-compatible` 會讓三項檢查一起失敗，其中一項的訊息就是
  `.../chat/completions`，也就是 405 的成因本身。
- 那張表是抄 OpenCode binary 的行為，不是呼叫 OpenCode。**這份測試不能證明公司
  gateway 接受請求**：它不開 socket、沒有 key、也沒有裝 `@ai-sdk/*`。真的往返仍然只有
  `docs/runbook.md`「人工 release gate」能驗 —— 這一項的驗法就是在 deployment host
  重跑上面那個 `opencode run --pure` 最小推論，確認不再是 405。
- 這張表現在只有兩個 adapter。要換成第三個（例如某天走 `@openrouter/...`）必須先補表，
  否則 `tests/provider-route.py` 會直接說它不認得這個 adapter 並失敗。這是刻意的：
  沒有人工確認過 route 的 adapter 不該悄悄上 production。
- Claude specialist 不改變這個路由；`COMPANY_GATEWAY_*` 只給
  `company-image` 用（見 [0008](0008-restricted-company-image-cli.md)）。

## Implementation update

2026-09-11：provider 多宣告一個 `gpt-6-astra`，並補上當初刻意不加的 model metadata
（`reasoning`、`tool_call`、`attachment`、`limit`）。

上面 Decision 說「不加這些 metadata，這次只修已診斷的那一個欄位」—— 那句話在只有 terra 與
luna 的時候成立：兩個都是 272k context，OpenCode 用預設值推導不會差太多。astra 是
1,050,000 context，不宣告 `limit` 等於讓 OpenCode 拿預設值去切一個十倍大的窗，該送的 context
會被自己截掉。所以這次連同 terra、luna 一起把 metadata 補齊，四個欄位的值抄自本機 chezmoi
那份已經在用的設定。

沒有變的：`npm` 仍然是 `@ai-sdk/openai`，`baseURL`／`apiKey` 仍然是 `{env:...}` 引用，
`COMPANY_GATEWAY_BASE_URL` 的語意（gateway 提供 `/responses` 的前綴）也沒變。
`tests/provider-route.py` 的 model ID 集合跟著加 astra —— 它驗的仍然是 route，不是 gateway
收不收；astra 在公司 gateway 上到底有沒有 expose，只有 `docs/runbook.md` 那條人工 release
gate 能證明。
