# session 中途接的 MCP server 走 mcpdo，不走 Codex 的 MCP 設定

## Status

Accepted（2026-11-06）。補 [0012](0012-follow-the-official-openab-codex-deployment.md)。

## Background

同事要讓 company agent 操作自己 Windows 上的桌面 app（[#45](https://github.com/henry5720/agent-company/issues/45)）：
他照 agent-config 的 windows-mcp skill 在自己的 Windows 起 server，用 expose-port 開成 quick tunnel，
再在 DM 把公開網址和 Bearer key 交給 agent。agent 要在這個 session 裡接上這個 MCP server，
截圖、點擊、打字，截圖還要回 thread。

Codex 自己的 MCP 接法在這裡接不上，2026-11-06 對 Codex 0.166.0、codex-acp 2.5.0 的原始碼與 CLI 確認過：

- **MCP 只在 session 開始時載入。** `codex mcp add --url ... --bearer-token-env-var` 寫的是
  `~/.codex/config.toml`，跑著的 session 看不到。重載要 ACP client 送 `config/mcpServer/reload`，
  codex-acp 只在使用者打 `/mcp reconnect` 時送；agent 自己送不了，Slack 也會把 `/` 開頭的訊息當成 Slack 指令。
- **key 進不了 Codex 的 env。** Codex 只支援從環境變數讀 Bearer（`bearer_token_env_var`）。codex-acp 的
  env 由 OpenAB 在啟動時給定（`config/openab.toml` 的 `env`、`inherit_env`），同事在對話裡交的 key
  放不進去。改寫成 `http_headers` 會把 key 寫進共用 home 裡的 `config.toml`，`agent-sync` 跟 skillshare
  也會跟這段條目打架（只保留 skillshare 寫的 `[mcp_servers.*]`）。
- **截圖回不了 thread。** MCP tool 回的圖片交給模型看，不會落成檔案；`slack-thread-artifact` 只收 drafts 下的檔案。

## Decision

image 固定版本裝 MCP Inspector（`@modelcontextprotocol/inspector`，版本在 `config/versions.env` 的
`MCP_INSPECTOR_VERSION`），agent 用它的 `mcpdo` CLI 從 shell 接：

```text
mcpdo connect <公開網址>/mcp --header "Authorization: Bearer <key>" --elicit off
mcpdo --format json --connection <公開網址>/mcp tools/call Screenshot   # 圖片是 base64，解成 PNG 放 drafts
mcpdo disconnect <公開網址>/mcp
```

build 時驗版本與 `mcpdo` 在 PATH 上，deploy 驗它跑得起來，跟 playwright-cli 同一個模式。
key 只出現在 `connect` 的參數，留在 mcpdo daemon 的記憶體，不寫進 home。

2026-11-06 本機 build 測試 tag 的 image，`--network none`，在裡面起一個只綁 127.0.0.1 的假 MCP server
（streamable HTTP、Bearer、SSE 回應、只收 loopback `Host`，模仿 windows-mcp）驗過：`connect` 成功、
`tools/list` 列出 tool、`tools/call Screenshot` 解出合法 PNG、錯的 key 回 `auth_required`、
`/home/node` 底下找不到 key、`disconnect` 後連線清單是空的。

沒選的做法：

- **在 container 預放 MCP 設定**：解決不了上面三點；網址每次不同（quick tunnel），也沒有固定的 key 可放。
- **agent 自己用 curl 拼 JSON-RPC**：做得到（windows-mcp 的 quick tunnel 實測就是這樣打的），但
  session id、SSE 解析、圖片解碼每次都要重寫，容易錯。mcpdo 是 MCP 官方 Inspector 附的 agent 用 CLI。
- **自己寫一支 stdlib MCP client 放 `agents/bin/`**：比較小，但要自己維護協定細節；官方已經有。

## Consequences

- image 多約 100 MB（Inspector 帶 web UI 的依賴）。只用 `mcpdo`，web UI 不開。
- mcpdo daemon 在共用 home，連線名稱就是網址；接上期間，其他 thread 也叫得到這條連線。跟 #43 接瀏覽器
  的取捨一樣，只在 [bot-guide](../bot-guide.md#讓它操作你的-windows-桌面-app) 提醒，不加限制。
- key 會留在 DM 的對話與 Codex 的 session 紀錄裡（home volume）。所以每次用完都要同事收掉 server、換 key，
  照 windows-mcp skill 的規則：用聊天訊息交的 key，用完一定換。
- 往後同事要交其他 MCP server（公開網址加 Bearer）也走同一條，不必改 image。
- 升級 Inspector 時看 `mcpdo --help` 的 `connect`、`tools/call` 參數有沒有變，並重跑上面的假 server 驗證。
