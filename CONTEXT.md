# Work Agent Deployment

這個 context 定義 workspace agent 與 Slack 互動資料的領域詞彙和關係。

## Language

**workspace agent**:
在 Slack 給 PM 共用的 agent。它拿 repo snapshot 當參考做 prototype、回答產品問題、讀寫待辦；
產物回 Slack thread 當參考，不進 repo，也不 push。
_Avoid_: product-context bot、remote executor

**product context**:
描述產品現況、決策、限制與相關來源的共享脈絡。workspace agent 以它作為回答的依據。
_Avoid_: 個人工作筆記

**repo snapshot**:
host 定期從 GitHub 同步、所有 thread 共用的唯讀 repo 視圖，停在基準 branch。它是 product
context 的工程來源之一，也是 thread workspace 的 clone 來源；agent 對它只有讀取。
_Avoid_: 可寫 snapshot、在 snapshot 上 checkout

**thread workspace**:
一個 Slack thread 專屬、可寫的 repo 工作副本，以 `<channel_id>-<thread root ts>` 識別，一次一個
repo，從建立當下的 repo snapshot 而來。要改 code 時才建，不自動跟 snapshot 同步，久未修改會被清掉。
_Avoid_: worktree、session dir、sandbox

**prototype sandbox**:
執行 repo code（install、dev server、截圖、附件轉換）的另一個 container，不帶任何 token；它唯讀讀
thread workspace，只寫自己的產物目錄。共 2 格。
_Avoid_: 單講「sandbox」（會跟 Codex 的 sandbox mode 混在一起）

**app prototype**:
在 thread workspace 改真的 app、由 prototype sandbox 起 dev server 後截圖交付的 prototype。預設做這種。

**HTML prototype**:
單一檔案、不連外部資源的 HTML 頁面，直接上傳到 thread。現有 app 跑不出來、PM 要單獨檔案，或只演示
邏輯與流程時才做。單說「prototype」指 app prototype 與 HTML prototype 兩者統稱。

**Slack query scope**:
Slack 查詢的來源範圍。預設是 current thread，也可以是綁定的 List，或使用者明確提供的
permalink／channel 來源；workspace agent 不把其他對話自動視為查詢來源。
_Avoid_: 全域搜尋、隱含跨 thread 查詢

**native attachment**:
Slack 直接理解的輸入是文字、圖片與音訊附件。PDF、DOCX、XLSX、PPTX 由 prototype sandbox 下載並轉成
Markdown 後才讀（真實 OpenAB／R2 附件未實測）；video、ZIP 不支援。
_Avoid_: 未宣告的附件格式

**image artifact**:
`company-image` 產生或編輯的 PNG，一定先落在 `/home/node/drafts/<UTC 日期>/`。上傳成功後立即刪除；
上傳失敗時保留給 host 每小時的 cleanup，壽命 24 到 25 小時（保留期限 24 小時，但檢查每小時才一次）。
_Avoid_: 把截圖或 HTML prototype 也叫 image artifact、永久保留暫存、宣稱剛好 24 小時

**sender context**:
OpenAB 寫進 prompt 的當次 sender、`channel_id` 與 `thread_ts`。它是 prompt content，不是 ACP
side-channel，也不是不可竄改的授權資料。「產物回原本那個 thread」是 OpenAB、agent 與受限 CLI 在
**同一個 container** 內的信任約定，靠 `agents/CLAUDE.md` 的規則維持，不是可驗證的安全保證。
_Avoid_: 說成身分驗證、token isolation、防 prompt injection、broker 或 relay

**company-image**:
container 內生圖／改圖唯一有人維護的入口。它是一支受限 CLI：mode 只有 `generate` 與 `edit`，參數只有
prompt、允許清單內的 size 與來源圖片，沒有 endpoint、header、model 或輸出路徑。請求經 host proxy 送到
公司 gateway，key 不在 container 裡，所以它不是安全邊界，是固定、可測的正常路徑。
_Avoid_: broker、upload helper、任意 gateway 呼叫

**skill catalog**:
public repo `agent-config`（由 skillshare 管理）的 skills／MCP 定義 catalog；其中 `skills/` 整個目錄以唯讀
mount 進 container。部署層不裁 catalog，Codex 也沒有 skill deny 設定，所以「skill 在 container 內讀得到」
不等於 runtime contract 允許 bot 使用。真正的允許清單只有 `agents/CLAUDE.md`；新增 bot 能力要 review
contract、mount 與 Slack Home，不在這裡另造一份 skill 清單。
_Avoid_: skill registry、registry allowlist、catalog 等於授權

**agent runtime**:
container 內實際接 ACP 的程式：OpenAB 直接啟動 `codex-acp`，以 agent-full-access mode 執行，經 host proxy
打公司 gateway。可寫範圍由 Docker mount 決定，不由 runtime 的 permission 決定。
_Avoid_: 把 runtime mode 當成安全邊界、runtime npx download

**runtime secret**:
container 內 agent process 讀得到的憑證：Slack bot token 在 agent env；OpenAB 自己的 Slack app token、
R2 key 與（啟用時的）STT key 在 OpenAB process env，同 uid 的 agent 推論讀得到。公司 gateway key 不是
runtime secret：它只在 host proxy。這些 secret 共用同一個失效邊界，沒有 broker、也沒有 token 隔離層。
_Avoid_: 已隔離、broker、upload helper
