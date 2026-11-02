# 對 gateway 送 Codex 的 originator

## Status

Accepted（2026-11-02）。補 [0012](0012-follow-the-official-openab-codex-deployment.md)。

## Background

2026-11-02 Slack 上的 bot 不定期回：

```
stream disconnected before completion: The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.
```

公司 gateway 背後是多個 ChatGPT 帳號。維護者機器用同一把 key、同一個 base_url、同一個 model，
卻沒有遇到這個錯誤。

唯一的差別是 `originator` header。`codex exec` 送 `codex_exec`；bot 走 codex-acp →
`codex app-server`，app-server 拿 ACP client 名稱當 originator，也就是 `openab`。

同一個 image（Codex 0.166.0、codex-acp 2.5.0），在拋棄式 container 裡的實測結果：

| 路徑 | originator | 結果 |
|---|---|---|
| `codex exec` | `codex_exec` | 9/9 成功 |
| codex-acp `session/prompt` | `openab` | 11 次中 6 次 `model_not_found` |
| codex-acp `session/prompt`，加 `CODEX_INTERNAL_ORIGINATOR_OVERRIDE=codex_exec` | `codex_exec` | 5/5 成功 |

## Decision

`config/openab.toml` 的 `[agent].env` 加 `CODEX_INTERNAL_ORIGINATOR_OVERRIDE = "codex_exec"`。
`tests/static.sh` 斷言這個值存在。

## Consequences

- 這個 env 是 Codex 的 internal 設定，不是公開介面。升級 Codex 後要重跑上表的 ACP 實測；
  覆寫失效時，錯誤會重新出現。
- gateway 的用量統計會把 bot 算成 `codex_exec`，分不出 bot 和維護者的 `codex exec`。
- gateway 修好帳號分配後（非 Codex originator 也能穩定使用 `gpt-6.1-sol`），可以拿掉這個覆寫。
