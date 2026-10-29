# Codex 經 host proxy 打公司 gateway，key 不進 container

## Status

Accepted（已決定）。Supersedes [0010](0010-company-provider-uses-the-responses-api.md)、
[0011](0011-subagent-permission-prompts-hang-the-turn.md)。來源：spec #21 §4、§9。

## Context

[0012](0012-shared-workspace-agent-for-pms.md) 讓 agent 在 container 內 full access。key 只要在
container 裡，agent 就讀得到、也能帶出去。同時 runtime 從 `opencode acp` 換成 `codex-acp`
（OpenAB 留著，不換 bridge），0010、0011 記的 OpenCode 設定都不再存在。

## Decision

- **key 不進 container。** host 上的 Caddy（`config/caddy/Caddyfile`，綁 bridge IP
  `10.231.0.1:18080`）只放行 `POST /responses`、由它補上 `Authorization`，其他路徑 403。
  key 只在 host 的 `/etc/work-agent/gateway.env`。bot 的 `COMPANY_GATEWAY_BASE_URL` 與 Codex
  `base_url` 都是 proxy 位址；位址正本 `config/host-network.env`。防火牆只讓 bot（固定
  `10.231.0.10`）連得到 proxy，sandbox 連不到。
- **gateway 只開 Responses API**（取代 0010）：`config/codex/config.toml` 的
  `wire_api = "responses"`。打到 `/chat/completions` 會被 proxy 擋成 403、被 gateway 回 405。
  `company-image` 走同一個 proxy、同一個 `/responses`。
- **`INITIAL_AGENT_MODE=agent-full-access`**（取代 0011）：container 內 codex 的 bwrap 建不了
  namespace，預設 `agent` mode 每條 shell 都失敗；`workspace-write` 等非 full-access mode 每條都要
  人按允許，而 Slack 上沒有人會按，那一輪就停住（0011 是同一個問題在 OpenCode 上的樣子）。
  可寫範圍交給 Docker mount 決定。OpenAB 不呼叫 `session/set_mode`，初始 mode 只能用 env。
- `CODEX_HOME` 用 named volume：沒有它 `session/load` 失敗，OpenAB 會直接開新 session、不告訴
  使用者。`config.toml` 單檔唯讀疊在 volume 上；model catalog（只放 `gpt-6.1-sol` 一筆，消掉每輪
  回覆開頭的 model metadata warning）唯讀掛 `/etc/codex/catalog.json`。
- codex 與 codex-acp 釘版裝在 `/opt/codex-<ver>`，`CODEX_PATH` 指過去；兩個一起升，升級時重產
  catalog。三個 env 都從 `config/openab.toml` 的 `[agent].env` 給。

## Consequences

- container 拿不到 key，但經 proxy 仍然可以花額度。用量靠 proxy 的 JSON access log（保留 14 天，
  沒有 thread、使用者、model 欄位）與 `scripts/alerts.py` 的 `/responses`、生圖告警看。
- 回滾到舊 bot 時要把 key 貼回 `env/openab.env`（`docs/runbook.md`「切換與回滾」）。
- `docs/runbook.md` 的人工 release gate 要真的從 Slack 經 OpenAB 跑一輪：靜態檢查只證明設定值。
