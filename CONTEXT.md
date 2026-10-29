# agent-company

這個 context 定義 company agent 與 Slack 互動資料的領域詞彙和關係。

## Language

**company agent**:
在 Slack 給公司同事共用的 coding agent。它拿 repo snapshot 當 product context 回答產品問題；需要時
clone 一份 repo 到自己的 home 修改、安裝、執行，再把結果（說明、patch、截圖、檔案）交回 Slack thread。
它送不回 GitHub。
_Avoid_: product-context bot、workspace agent、remote executor

**product context**:
描述產品現況、決策、限制與相關來源的共享脈絡。company agent 以它作為回答的依據。
_Avoid_: 個人工作筆記

**repo snapshot**:
host 定期從 GitHub 同步、所有 thread 共用的唯讀 repo 視圖，掛在 `/home/node/snapshots`，停在基準
branch。它是 product context 的工程來源，也是 agent clone 工作副本的來源；agent 對它只有讀取。
唯讀是為了正確性（共用、會被同步蓋掉），不是安全邊界。
_Avoid_: 可寫 snapshot、在 snapshot 上 checkout

**agent home**:
`/home/node`，一個持久、可寫的 named volume，照 OpenAB 官方的 PVC 做法。Codex 的 session、agent
clone 的工作副本、自己安裝的工具都在這裡；所有 thread 共用，沒有自動清理。
_Avoid_: thread workspace、sandbox

**patch**:
agent 把改好的 code 交回人的形式：`git format-patch` 產出的 `.patch`，用 `slack-thread-artifact`
上傳到 thread。人在自己的電腦套用、review、push。
_Avoid_: bot push、bot 開 PR

**Slack query scope**:
Slack 查詢的來源範圍。預設是 current thread，也可以是綁定的 List，或使用者明確提供的
permalink／channel 來源；company agent 不把其他對話自動視為查詢來源。
_Avoid_: 全域搜尋、隱含跨 thread 查詢

**image artifact**:
company agent 透過 `company-image` 向公司 gateway 產生或編輯的圖片產物。格式一定是 PNG，
一定先落在 `/home/node/drafts/<UTC 日期>/`；只有使用者明確要求時，才改輸出 self-contained HTML
prototype。artifact 上傳成功後立即刪除暫存；上傳失敗時保留給 host 每小時的 cleanup，壽命 24 到
25 小時（保留期限 24 小時，但檢查每小時才一次）。
_Avoid_: 未說明格式、永久保留暫存、bot 自己組 gateway 請求、宣稱剛好 24 小時

**sender context**:
OpenAB 寫進 prompt 的當次 sender、`channel_id` 與 `thread_ts`。它是 prompt content，不是 ACP
side-channel，也不是不可竄改的授權資料。「產物回原本那個 thread」是 OpenAB、agent 與受限 CLI 在
**同一個 container** 內的信任約定，靠 `agents/AGENTS.md` 的規則維持，不是可驗證的安全保證。
_Avoid_: 說成身分驗證、token isolation、防 prompt injection、broker 或 relay

**company-image**:
container 內生圖／改圖的唯一入口。它是一支受限 CLI：mode 只有 `generate` 與 `edit`，參數只有
prompt、允許清單內的 size 與來源圖片，沒有 endpoint、header、model 或輸出路徑。gateway 位置與
key 只從 runtime env 讀。
_Avoid_: broker、upload helper、任意 gateway 呼叫

**skill catalog**:
public repo `agent-config`（由 skillshare 管理）裡的 skills、MCP 與個人規則。company agent 跟維護者機器用
同一份：`agent-sync` 在 container 裡跑 skillshare 裝進 Codex，並把個人規則接上 `agents/AGENTS.md` 的 bot
專屬規則。部署層不另裁，也不另造一份 skill 清單。
_Avoid_: skill registry、bot 專用 allowlist

**agent runtime**:
container 內實際接 ACP 的 CLI：OpenAB 跑 `codex-acp`，mode 是 `agent-full-access`，模型與 provider
在 [`config/codex/`](config/codex/)。照 OpenAB 官方 Codex 部署，邊界是 container 本身。
_Avoid_: runtime npx download、Codex 內層 sandbox

**runtime secret**:
container 內 agent process 拿得到的憑證：Slack bot token 與公司 gateway key。它們跟 agent 執行的
repo code 共用同一個失效邊界：沒有 broker、proxy 或 token 隔離層。container 裡沒有 GitHub credential。
_Avoid_: 已隔離、broker、upload helper

**Slack scope security debt**:
功能先上線不等於現有 Slack 實作已完成縮權。現有 Slack 過度 scope 是高優先 follow-up security
debt，必須持續追蹤至完成縮權。
_Avoid_: 已縮權、已完成安全邊界
