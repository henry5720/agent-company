# 定義唯讀 product-context／handoff bot

## Status

**Superseded by [0012](0012-shared-workspace-agent-for-pms.md)。** 保留原文說明當初為什麼唯讀。

Accepted（已決定）。Supersedes [0005](0005-separate-backlog-and-execution-agents.md)。
Amended by [0007](0007-single-container-opencode-runtime.md)：image-artifact broker 不做，
runtime secret 共用單一 container，版本改由 `config/versions.env` 集中固定。
Amended（2026-09-26）：handoff／issue 草稿已移除，bot 只回答產品問題。

## Background

目前的設計把 Slack bot 與遠端執行工作放在同一條演進路線上，造成產品脈絡查詢、交接整理與
repo 寫入之間的邊界不清。這個 bot 的產品責任是提供唯讀的 product context 與 handoff pack，
不是把人對本機工具的使用方式搬到遠端，也不是替人執行 repo 工作。

## Decision

- bot 定位為唯讀 product-context/handoff bot，不是 remote executor。它維持 readonly snapshot，
  禁止 repo edit、Git mutation、shell 任意網路、Docker socket、可寫 checkout、patch 與
  repo ZIP。
- 遠端使用單一 OpenAB container，以 OpenCode 搭配 oh-my-opencode-slim（OMO）remote profile。
  remote profile 與本機 profile 獨立，不複製個人 paths、tools 或 secrets。
- OMO 的模型分工固定為 Luna retrieval、Terra synthesis；Claude ACP 僅作 specialist，
  不是預設執行層。OMO 每輪不設 hard cap，但必須記錄 model calls、subagent calls 與 image calls
  14 天，並對異常用量告警。
- Slack v1 只支援 native text、image、audio；PDF、Office、video、ZIP 不支援。
- Slack 查詢預設只看 current thread；其他來源限於綁定的 List，或使用者明確提供的 permalink
  或 channel 來源。
- skills 的來源是 `work-helper` catalog。（[0007](0007-single-container-opencode-runtime.md)
  改為不在部署層裁 catalog：整個 skills 目錄仍唯讀 mount，跑不動或不該跑的 skill 由
  `agents/CLAUDE.md` 用文字擋，不用 registry allowlist 表達。）
- 生圖與編輯走公司 gateway，產物固定回到原 Slack thread。圖片預設輸出 PNG；只有使用者明確
  要求時才輸出 self-contained HTML prototype。artifact 上傳成功即刪除暫存；上傳失敗最多保留
  24 小時後刪除。（[0007](0007-single-container-opencode-runtime.md) 取消原本的
  image-artifact broker：同一個 container 內完成，gateway key 與 Slack token 共用這個
  container。）
- 每次回覆提供 thread 摘要，並以 Markdown 產出 handoff；handoff 是交接內容，不是 repo patch
  或完成宣告。
- 功能先上線；但現有 Slack 實作的過度 scope 不得描述成已完成縮權，列為高優先 follow-up
  security debt，直到後續縮權完成。

## Consequences

- product-context bot 可以在明確的 Slack 來源內查詢 product context，並整理 handoff pack，
  但不會把查詢能力延伸成 repo 寫入或遠端執行能力。
- readonly snapshot、禁止可寫 checkout 與禁止 patch／repo ZIP，讓 bot 的輸出不會直接成為
  repo 變更；需要實作時仍由人或其他受控流程接手。
- 單一 OpenAB container 與獨立 remote profile 讓遠端環境不繼承本機個人 paths、tools、secrets，
  但也代表兩邊的設定與能力必須分別維護。
- native attachment 範圍明確，PDF、Office、video、ZIP 會留在支援範圍外，而不是進入未定義的
  解析流程。
- 以 PNG 作為 image artifact 預設格式可保持一般交付簡單；self-contained HTML prototype 必須
  由使用者明確要求。上傳成功即刪除暫存，失敗最多保留 24 小時，降低產物殘留風險。
- 圖片生成／編輯與原 Slack thread 的 artifact 上傳都在同一個 container 完成，暫存清理責任
  留在 runtime 與 agent 規則，沒有另一層 process 幫忙擋。
- OMO 不設每輪 hard cap 會保留完成複雜工作的彈性，但 14 天的 model、subagent、image calls
  記錄與告警是用量異常的監測依據。
- 功能先上線可先提供價值，但現有 Slack 過度 scope 是高優先 follow-up security debt，不是已
  完成的縮權成果。
