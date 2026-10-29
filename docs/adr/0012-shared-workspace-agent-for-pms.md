# 把唯讀 product-context bot 改成 PM 共用的 workspace agent

## Status

Accepted（已決定）。Supersedes [0006](0006-readonly-product-context-handoff-bot.md)、
[0007](0007-single-container-opencode-runtime.md)。來源：spec #21 §1、§3、§9。

## Context

唯讀 bot 只能回答「現在的 code 長怎樣」。PM 真正要的是「改成這樣會長什麼樣」：拿 repo
snapshot 當參考做 prototype、看截圖。唯讀邊界（0006）讓這件事只能回到維護者手上做。

## Decision

- bot 改成給 PM 用的共用 **workspace agent**。使用者只有 PM；維護者自己的改碼與 push 留在 local。
- agent 在 container 內 full access（shell、網路）。container 擋宿主機，不擋 container 內的東西：
  不 privileged、不掛 Docker socket、不可寫掛宿主機目錄；可寫的只有 thread workspace、drafts、
  CodeGraph `.index` 與 `CODEX_HOME` volume，repo snapshot 維持唯讀。
- 共用 bot **不給任何 GitHub 寫入**：本地 git 可用（commit 只為了做 diff）、不能 push、image 不裝 `gh`。
- 照 OpenAB「一個 bot 一個 pod」：Compose 單一 bot container。thread 之間只分開 session
  （process），檔案與權限共用。這是接受的取捨。
- 沿用維護者的開發機當 deployment host，不另開專用機器（維護者 2026-10-27 決定）。
- repo 的 code（install、dev server、截圖）不在 bot 跑，交給 prototype sandbox，見
  [0014](0014-run-repo-code-in-the-prototype-sandbox.md)。runtime 與 gateway key 的位置見
  [0013](0013-codex-reaches-the-gateway-through-a-host-proxy.md)。

## Consequences

- deployment host 上有可 push 的 `gh` token（`repo` scope）、個人 SSH key 與 Claude 登入憑證。
  agent 與它們之間只隔 container 邊界，container 被逃出就等於拿到維護者的 GitHub 權限。
- 同一個 container 裡所有 thread 共用 Slack bot token 與 OpenAB 的 secret；被 prompt injection
  的 thread 讀得到別的 thread 的檔案。runtime secret 清單與縮權見 `docs/system-design.md`
  「Runtime secret」。
- 0006 寫的「記錄 model、subagent、image calls 14 天並告警」在 repo 裡**從來沒有實作**。
  用量觀測改由 host Caddy access log（14 天）與 host 告警 cron 做（`scripts/alerts.py`），
  是從零定的，不是取代既有機制。
- 0006 的 Slack scope security debt 由這次縮到 10 個 scope 處理（`docs/runbook.md`「設定 Slack App」）。
