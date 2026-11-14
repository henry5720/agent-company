# 瀏覽器照閒置回收：每個 thread 一個 isolated context，不是每個 thread 一個瀏覽器

## Status

Accepted（2026-11-14）。補 [0012](0012-follow-the-official-openab-codex-deployment.md)。

## Background

`chrome` sidecar 所有 thread 共用，agent 用 `playwright-cli attach --cdp=http://127.0.0.1:9222` 操作它。
2026-11-14 在 deployment host 上看到的狀況：

- 6 個 `cliDaemon` 活了 20 小時到 3 天，每個 400–700MB。它們的父程序是 tini：
  playwright-cli 用 detached spawn 起 daemon，叫它的 Codex session 被 OpenAB 回收後，daemon 繼續活著。
- `attach --help`（0.1.23）寫明「attached browsers are never detached by default」。原始碼裡預設的
  1 小時閒置只套用在 playwright-cli 自己開的 headless 瀏覽器（`ownership === "own"`），不套用在 attach。
- `close`／`detach` 只結束 daemon、斷線。分頁開在 Chrome 的 default context，斷線後留在 sidecar，
  只有 `tab-close` 會關。
- 所有 thread 都用同一個 default context，cookie 與登入互通：A thread 登入過的網站，B thread 直接沿用。

OpenAB 的 `[pool] session_ttl_hours` 是閒置 TTL（`pool.rs` 的 `last_active < cutoff`），執行中的 session 不收。
OpenAB 只有 `pre_boot`、`pre_seed`、`pre_shutdown` 三種 hook，沒有「session 被回收」可以掛。

## Decision

回收的單位是 thread 的 isolated context，條件是**閒置**，不是存活時間：

1. **SOP**（`agents/AGENTS.md`）：thread 用 `-s=t-<thread_ts>` 的 session，分頁用
   `tab-new --isolated-context=t-<thread_ts>`，做完 `close`。
2. **閒置自動收**（`config/openab.toml` 的 `PLAYWRIGHT_MCP_IDLE_TIMEOUT = "14400000"`）：跟
   `session_ttl_hours` 一樣 4 小時，每個指令重新計時。到時 daemon detach 並結束。
3. **兜底**（`scripts/reap-browser-tabs.sh`，每小時 :37）：只看 default context，同一個分頁連續 4 小時
   網址都沒變就關。isolated context 不碰。

2026-11-14 在 sidecar 上實測（`PLAYWRIGHT_MCP_IDLE_TIMEOUT=20000`）：離 attach 26 秒、離上一個指令 14 秒時 daemon 還在；
閒置滿 20 秒後 daemon 結束，它的 isolated context 與分頁一起消失，default context 的分頁不受影響。
主動 `close` 也一樣會關掉 isolated context。

沒選的做法：

- **每個 thread 一個瀏覽器**：image 沒有瀏覽器依賴，要另起 sidecar 或 browser pool（browserless 這類）。
  isolated context 在同一個 Chrome 裡就能隔離 cookie，代價只有分頁本身的 renderer。
- **cron 照存活時間殺 daemon**：從外面量不到 daemon 最後一次收到指令的時間（`.session` 只在啟動時寫），
  照存活時間殺會砍到正在跑的長任務。閒置判斷交給 daemon 自己的計時器。
- **只靠 SOP**：bot 中途出錯或 session 被回收時一定會漏，漏掉的就是現在看到的那些 daemon。
- **在部署層禁用 chrome-devtools MCP 或限制分享畫面**：內部使用，能力與 SOP 屬於 agent-config，
  部署層不另裁（`CONTEXT.md`「skill catalog」）。這裡只補資源回收。

## Consequences

- 同一個 thread 閒置超過 4 小時再用瀏覽器，要重新 attach、重新登入。
- 每個 thread 要自己登入；登入狀態不再跨 thread 共用。
- 兜底只能猜：在 default context 停在同一頁超過 4 小時的分頁，即使有人在用也會被關。
  照 SOP 開的分頁都在 isolated context，不受影響。
- 升級 playwright-cli 時重驗兩件事：閒置計時是否每個指令重算、detach 是否關掉 isolated context。
