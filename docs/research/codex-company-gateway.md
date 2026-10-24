# Codex 怎麼接公司 gateway（含 host proxy 與原生生圖）

對應 issue #4。只做原始碼與官方文件研究，**沒有呼叫公司 gateway**。

## 結論

| 問題 | 結論 |
|---|---|
| 1. Codex／codex-acp 能不能指到公司 gateway | **可以。** 在 `config.toml` 定義一個自訂 `model_providers.<id>`，`base_url` 填 `COMPANY_GATEWAY_BASE_URL` 那個前綴、`wire_api = "responses"`。Codex 打 `{base_url}/responses`，用 HTTP + SSE。 |
| 2. host reverse proxy 注入 Authorization | **可以，而且 container 內連 dummy key 都不用。** 自訂 provider 不設 `env_key`、`requires_openai_auth` 保持 `false` 時，Codex 完全不送 `Authorization`。自訂 provider 預設不走 websocket，也不壓縮 request body，所以 proxy 只要轉一條 `POST …/responses` 並且不 buffer SSE。推薦 Caddy。 |
| 3. Codex 原生生圖能不能取代 `company-image` | **不能。** Codex 原生生圖不是 Responses API 的 `image_generation` tool，是 client 端的 `image_gen.imagegen` tool，打 Images API `POST {base_url}/images/generations`（或 `/images/edits`），model 寫死 `gpt-image-2`。而且只有 OpenAI 登入的 provider 才會註冊這個 tool。公司 gateway 只有 `/responses`，兩個條件都不符合。 |

## 版本基準

| 元件 | 版本 | 來源 |
|---|---|---|
| OpenAB `main`（`20830d5d`，2026-10-23）的 `Dockerfile.codex` | `@agentclientprotocol/codex-acp@2.0.0` + `@openai/codex@0.159.0` | `openab/Dockerfile.codex:30-32` |
| 本 repo 目前釘的 OpenAB `0.10.0-beta.3` 若換 codex variant | `codex-acp@1.1.4` + `codex@0.144.1` | `git show v0.10.0-beta.3:Dockerfile.codex` 第 24-25 行 |
| Codex 原始碼 | tag `rust-v0.159.0`（`687a119f0f`）；生圖另查 `rust-v0.144.1`（`44918ea10c`） | https://github.com/openai/codex |
| codex-acp 原始碼 | tag `v2.0.0`（`2eebebc`）與 `main`（`a77829f`） | https://github.com/agentclientprotocol/codex-acp |

以下 `codex-rs/...:行號` 都是 `rust-v0.159.0`，除非另外註明。clone 在 `$HOME/.cache/wayfinder/src/{codex,codex-acp,openab}`。

注意：OpenAB 的 Codex 已經換成 `agentclientprotocol/codex-acp`（TypeScript，底下跑 `codex app-server`），不是舊的 `zed-industries/codex-acp`（Rust）。OpenAB 文件寫明 `-c key=value` 參數會被**靜默忽略**（`openab/docs/codex.md`「Breaking change — `-c` CLI overrides are silently ignored」），所以 provider 設定只能走 `config.toml` 或 `CODEX_CONFIG` env。

## 1. 把 provider 指到公司 gateway

### 設定

```toml
# /home/node/.codex/config.toml（user-level；project-level .codex/config.toml 會忽略 provider 相關 key）
model_provider = "company"
model = "gpt-6.1-sol"

[model_providers.company]
name = "Company Gateway"
base_url = "https://gateway.example/<提供 /responses 的前綴>"   # = COMPANY_GATEWAY_BASE_URL，不必是 /v1
wire_api = "responses"                 # 唯一合法值，也是預設
env_key = "COMPANY_GATEWAY_API_KEY"    # 直連時才需要；走 proxy 時刪掉，見第 2 節
# query_params = { api-version = "..." }  # gateway 要求才加
# http_headers / env_http_headers       # gateway 要額外 header 才加
```

### 每個欄位在原始碼裡的行為

- **provider id 不能用 `openai`**：`openai`、`ollama`、`lmstudio` 是保留 ID（官方 [config-reference](https://learn.chatgpt.com/docs/config-file/config-reference)、[config-advanced](https://learn.chatgpt.com/docs/config-file/config-advanced)）。官方也提供 `openai_base_url`，只改內建 `openai` provider 的 URL。**這裡不要用它**：內建 openai provider 是 `requires_openai_auth: true`、`supports_websockets: true`（`codex-rs/model-provider-info/src/lib.rs:552-553`），會要求登入，也會先嘗試 websocket。
- **`wire_api`**：enum 只剩 `Responses`（`model-provider-info/src/lib.rs:100-107`）。寫 `"chat"` 會直接報錯，錯誤訊息要你改成 `responses`（`:96`、`:126`）。這跟 gateway「只提供 Responses」剛好吻合，不會重演 ADR 0010 的 405。
- **URL 組法**：`Provider::url_for_path` 是 `base_url` 去掉結尾 `/`，接 `/` 和 path，再把 `query_params` 用 `?k=v&…` 接在後面（`codex-rs/codex-client/src/provider.rs:55-77`）。Responses 請求的 path 是 `"/responses"`（`codex-rs/codex-api/src/endpoint/responses.rs:137-148`），所以最後打的是 `{base_url}/responses`，跟 `agents/bin/company-image` 的 `gateway_endpoint()` 接一樣的後綴。注意 `query_params` 的值沒有做 URL encode（`provider.rs:64-73`），值要自己確保安全。
- **SSE**：請求固定帶 `Accept: text/event-stream`（`responses.rs:142-146`），用 HTTP streaming 讀。這跟實測 gateway 回 SSE 一致。
- **`env_key`**：只要設了，就從該 env 讀 key，空字串或沒設就**報錯**（`model-provider-info/src/lib.rs:472-487`），然後包成 `Authorization: Bearer <key>`（`codex-rs/model-provider/src/auth.rs:292-305`）。
- **`experimental_bearer_token`**：把 key 直接寫進 config，官方說不建議用（`model-provider-info/src/lib.rs:150-153`）。
- **`http_headers` / `env_http_headers`**：靜態 header 和從 env 取值的 header；env 沒設或是空的就不送那個 header（`model-provider-info/src/lib.rs:389-416`）。
- **`requires_openai_auth`**：預設 `false`。`false` 表示跳過登入流程，key（如果需要）從 `env_key` 取（`model-provider-info/src/lib.rs:184-188`）。
- **retry 與 timeout**：`stream_idle_timeout_ms` 預設 300000、`stream_max_retries` 預設 5、`request_max_retries` 預設 4（`model-provider-info/src/lib.rs:63-65`）。

### codex-acp 這一層

- codex-acp 開 session 前問 app-server `account/read`，只有 `requiresOpenaiAuth && !account` 時才要求登入（`codex-acp v2.0.0 src/CodexAcpClient.ts:347-358`；main 在 `:440-451`）。自訂 provider 不需要 OpenAI 登入，所以不會卡在 `authRequired`。
- codex-acp 也有「custom gateway」auth method（`baseUrl` + `headers`，內部轉成一個 `model_providers` 條目，固定 `wire_api = "responses"`）。但它只在 client 宣告 `clientCapabilities.auth._meta.gateway = true` 時才會出現（`src/CodexAuthMethod.ts:49-84`）。OpenAB 送出的是 `"clientCapabilities": {}`（`openab/crates/openab-core/src/acp/connection.rs:575-583`），所以這條路用不到，**要靠 Codex 自己的 `config.toml`**。
- 不寫 `config.toml` 的替代做法：codex-acp 讀 `CODEX_CONFIG`（JSON，merge 進 session config）與 `MODEL_PROVIDER`（`src/index.ts:43-47`，v2.0.0 也有，README「Runtime options」）。OpenAB 會先清空 env 再 spawn agent，這兩個值要放進 `[agent] env`／`inherit_env` 才傳得進去（`openab/docs/codex.md`「Agent Environment for Uploads」）。

### 模型 metadata

只有自訂 provider、沒有 ChatGPT 登入、沒有 `env_key`／command auth 時，Codex 不會去打 `{base_url}/models`（`codex-rs/models-manager/src/manager.rs:565-569`；自訂 `base_url` 的 `supports_api_key_models()` 是 false，見 `codex-rs/model-provider/src/models_endpoint.rs:231-234`）。它只用內建的 `models.json`。

`rust-v0.159.0` 內建的 slug 有 `gpt-6-astra`、`gpt-6-sol`、`gpt-6-luna`、`gpt-5.6-terra`、`gpt-5.6-luna`（context 都是 272000），**沒有 `gpt-6.1-sol`**。不認得的 slug 會套 fallback metadata（272k context，並記一筆 warn，見 `codex-rs/models-manager/src/model_info.rs:98-135`）。另外 Codex 的 `gpt-6-astra` 是 272k，本 repo `config/opencode/opencode.json` 宣告的是 1,050,000；要用大 context 得自己設 `model_context_window`。

## 2. host reverse proxy 注入 Authorization、container 內不放 key

### Codex 對 proxy 的要求（都從原始碼確認）

| 項目 | 自訂 provider 的行為 | 對 proxy 的意思 |
|---|---|---|
| Transport | websocket 只在 `supports_websockets = true` 時才啟用（`codex-rs/core/src/client.rs:1024-1031`）。自訂 provider 預設 `false`（`model-provider-info/src/lib.rs:189-191`） | 不用處理 websocket upgrade。**不要**設 `supports_websockets`。 |
| Method / path | 對話只打 `POST {base_url}/responses`。remote compaction 只開給 openai／Azure（`codex-rs/model-provider/src/provider.rs` 的 `capabilities()`），standalone web search 對自訂 provider 預設關閉（config-advanced） | proxy 只放行 `POST /responses`，其餘一律 403。 |
| Response | SSE，`Accept: text/event-stream`；閒置 300 秒才算斷線（`stream_idle_timeout_ms`） | proxy 不能 buffer，read timeout 要 ≥ 300 秒。 |
| Request body 壓縮 | 只有「openai provider + ChatGPT backend auth」才用 zstd（`core/src/client.rs:1617-1626`）；自訂 provider 是 `Compression::None` | proxy 不用處理 `Content-Encoding: zstd`。 |
| Authorization | 沒有 `env_key`、沒有 `experimental_bearer_token`、`requires_openai_auth = false`、沒有 `auth` 時，走 `UnauthenticatedAuthProvider`，**一個 auth header 都不加**（`model-provider/src/auth.rs:170-181`、`:197-220`） | container 內**不用 dummy key**。proxy 直接設定 `Authorization`。 |

所以 proxy 模式下的 container 內設定：

```toml
model_provider = "company"

[model_providers.company]
name = "Company Gateway (via proxy)"
base_url = "http://company-proxy:8080"   # proxy 對外的前綴；Codex 會打 /responses
wire_api = "responses"
# 不設 env_key、不設 requires_openai_auth、不設 supports_websockets
```

反過來說，如果留著 `env_key` 但 env 是空的，Codex 會直接報 `EnvVar` 錯誤（`model-provider-info/src/lib.rs:472-487`）。所以要嘛刪掉 `env_key`，要嘛給一個非空的 dummy 值讓 proxy 覆蓋。刪掉比較乾淨。

### 推薦 Caddy

選 Caddy：它對 `Content-Type: text/event-stream` 的回應會自動立刻 flush；`read_timeout`、`response_header_timeout` 預設不設上限；upstream 是 `https://` 時，Caddy v2.11.0 起會自動改寫 Host（[reverse_proxy 文件](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)）。header 可以用 `{env.X}` 在 runtime 讀 env。

不選 nginx 的原因：預設 `proxy_buffering on`、`proxy_read_timeout 60s`、`proxy_ssl_server_name off`（[ngx_http_proxy_module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)），三個都要手動改，而且 nginx 不能直接在設定裡讀 env（要另外用 template 或 envsubst 產設定）。

最小 Caddyfile（未實測）：

```caddyfile
:8080 {
	@responses {
		method POST
		path /responses
	}
	handle @responses {
		# Codex 打 /responses；改寫成 gateway 實際的前綴
		rewrite * {$COMPANY_GATEWAY_PATH_PREFIX}/responses
		reverse_proxy {$COMPANY_GATEWAY_ORIGIN} {
			header_up Authorization "Bearer {env.COMPANY_GATEWAY_API_KEY}"
		}
	}
	respond 403
}
```

`COMPANY_GATEWAY_ORIGIN` 是 `https://host[:port]`，`COMPANY_GATEWAY_PATH_PREFIX` 是 path 部分。兩者合起來就是現在的 `COMPANY_GATEWAY_BASE_URL`。`header_up` 用 set 語意，會蓋掉 client 送來的任何 `Authorization`。

如果一定要用 nginx，等價的最小設定：

```nginx
location = /responses {
    limit_except POST { deny all; }
    proxy_pass https://gateway.example/<prefix>/responses;
    proxy_set_header Host gateway.example;
    proxy_set_header Authorization "Bearer <由 template 產生>";
    proxy_ssl_server_name on;
    proxy_buffering off;
    proxy_read_timeout 360s;
}
location / { return 403; }
```

### Codex 官方的 `codex-responses-api-proxy`

Codex repo 裡有一個專門做這件事的 proxy：`codex-rs/responses-api-proxy/README.md`。它從 stdin 讀 key、`mlock` 住，只放行 `POST /v1/responses`，其他一律 403，並用 `--upstream-url` 指定上游。缺點有三個，所以不推薦：

- 只 bind `127.0.0.1`（`responses-api-proxy/src/lib.rs:138-143`）。從 Docker bridge 網路連不到，要嘛共用 network namespace（`network_mode: service:<proxy>`），要嘛 host network。
- 只接 `/v1/responses`，不接 query string（`lib.rs:170-173`），所以 container 內的 `base_url` 必須以 `/v1` 結尾。
- key 只允許 `[A-Za-z0-9_-]`（`responses-api-proxy/src/read_api_key.rs:211-217`），公司 key 的格式未確認。

### 部署位置

Codex 原始碼只決定「proxy 要長什麼樣」；proxy 放哪裡，原始碼沒有規定。比較省事的做法是把它放成 Compose 裡另一個 service（key 只出現在那個 service 的 env），bot container 透過內部網路連 `http://company-proxy:8080`，port 不 publish。如果一定要跑在 host 上，要 bind 在 docker bridge IP，bot container 加 `extra_hosts: ["host.docker.internal:host-gateway"]`，而且不能 bind `0.0.0.0` 對外開放。

### 對本 repo 其他元件的影響（要一起改，否則會壞）

- `agents/bin/company-image`：`gateway_key()` 要求 key 非空且不是 `replace-me`；`gateway_endpoint()` 拒絕非 localhost 的 `http://`（`agents/bin/company-image:107-127`）。改走 proxy 時，這兩處都要改，也要同步改 ADR 0008 和 `tests/image-runtime.py`。
- OpenCode provider（`config/opencode/opencode.json` 的 `apiKey: {env:COMPANY_GATEWAY_API_KEY}`）：`@ai-sdk/openai` 沒有 key 時會怎樣，這次**沒查**。可能需要一個 dummy key。
- 安全邊界的寫法：proxy 讓 key 拿不出 container，但 agent 仍然可以對 proxy 發任意 Responses 請求，包含 `image_generation` tool，費用照算。這只能寫成「key 不在 container 內」，不能寫成「agent 用不到 gateway」。本 repo CLAUDE.md 說 Slack token 和 gateway key 共用 container 是已決定的取捨，改成 proxy 等於改這個取捨，要另寫 ADR。

## 3. Codex 原生生圖

### 它走哪條 API

- tool 名稱是 namespace `image_gen` 底下的 `imagegen`（`codex-rs/core/src/tools/spec_plan.rs:105-106`），是 Codex 自己執行的 extension tool，不是送給模型的 hosted tool。
- 執行時用 `ImagesClient`，透過當前 provider 打 `POST {base_url}/images/generations`（edit 則是 `/images/edits`），見 `codex-rs/codex-api/src/endpoint/images.rs:62-81`、`codex-rs/ext/image-generation/src/backend.rs:79-97`。
- model 寫死 `gpt-image-2`（`ext/image-generation/src/tool.rs:58`），size 與 quality 固定 `auto`（`tool.rs:444-452`）。
- `rust-v0.144.1` 也一樣：`ext/image-generation/src/tool.rs:57` 是 `IMAGE_MODEL = "gpt-image-2"`，backend 也用 `ImagesClient`。

### 什麼時候這個 tool 會出現

`image_generation_available()`（`core/src/tools/spec_plan.rs:727-763`）要求全部成立：

1. feature `image_generation` 開啟。`0.159.0` 已經是 Stable 且預設開（`codex-rs/features/src/lib.rs:1548-1553`）。
2. 不是 Free plan。
3. provider capabilities 有 `image_generation` 和 `namespace_tools`。
4. model 的 input modalities 含 image。
5. provider 帶非空的 `x-openai-actor-authorization` header（`model-provider-info/src/lib.rs:619-627`），**或者** `requires_openai_auth = true` 且目前的登入走 ChatGPT／Codex backend。

`0.144.1` 的條件是 `is_openai() || requires_openai_auth || uses_openai_actor_authorization()`（`ext/image-generation/src/extension.rs:38-44`）。

公司 provider 是 `requires_openai_auth = false`、沒有 ChatGPT 登入，所以 **tool 根本不會註冊**。就算加一個假的 `x-openai-actor-authorization` header 讓它出現，請求還是會打到 `{base_url}/images/generations`，而公司 gateway 只提供 `/responses`，一樣不通（沒有實測，依 ADR 0008、0010 對 gateway 的描述）。

### 能否取代 `company-image`

不能，而且就算 gateway 之後開了 Images API，也不建議取代：

- `company-image` 的窄介面會消失：尺寸 allowlist、輸出只能在 `/home/node/drafts/<UTC 日期>/`、`--source` 只能來自允許的目錄。Codex 的 tool 由模型決定參數，size 是 `auto`，存檔位置是 Codex 的 save root 或 `cwd/generated_images`（`tool.rs:328-331`）。OpenAB 文件說的是 `~/.codex/generated_images/`。
- OpenAB 文件裡「`codex features enable image_generation` 就能生圖」的前提是 ChatGPT 登入（同一份文件的 Authentication 段用的是 `codex login --device-auth`）。

結論：Codex runtime 下生圖仍然要走 `company-image`（Responses `image_generation` tool）。Codex 只要被允許執行這支 CLI 即可。

## 沒查到或需要實測

- [ ] **gateway 收不收 Codex 的 Responses 請求 body**：Codex 會送 `reasoning`、`include`、`store`、`prompt_cache_key`、function／custom／namespace 類 tools、`parallel_tool_calls`，以及 `session_id`、`originator`、`x-codex-*` 之類的 header。需要在 deployment host 做一次真的往返。
- [ ] **未知 slug（`gpt-6.1-sol`）用 fallback metadata 的實際效果**：例如 tool 類型、reasoning 參數是否被 gateway 接受。
- [ ] **`config.toml` 能不能用 env 展開**：沒查到支援，目前假設 `base_url` 只能寫字面值。替代做法是用 `CODEX_CONFIG` env。
- [ ] **`config.toml` 用單檔唯讀 mount 會不會出事**：codex-acp `main` 會用 `config/batchWrite` 寫 hook trust（`src/CodexHookTrust.ts:37`），OpenAB 文件也警告不要把 ConfigMap 唯讀掛到 `config.toml`。
- [ ] **OpenCode `@ai-sdk/openai` 在 proxy 模式下沒有 key 時的行為**。
- [ ] **Caddy／nginx 設定**：沒有實際起過，SSE flush 行為只引用官方文件。
- [ ] **公司 key 的格式**：影響 `codex-responses-api-proxy` 能不能用。
- [ ] **OpenAB `0.10.0-beta.3` 的 codex variant（codex `0.144.1`）**：只核對了 provider 欄位存在（`rust-v0.144.1:codex-rs/model-provider-info/src/lib.rs:96,113,116,137,140`）和生圖條件，沒有逐條對 `0.159.0` 的其他行號。
