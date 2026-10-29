# 用單一受限 container 跑 OpenCode + OMO，加入 Claude ACP specialist

## Status

**Superseded by [0012](0012-shared-workspace-agent-for-pms.md)。** runtime 已換成 `codex-acp`、gateway key 已移出 container（[0013](0013-codex-reaches-the-gateway-through-a-host-proxy.md)）。

Accepted（歷史決策）；部署 rollback 與 wrapper 強制入口已由簡化方案取代。

## Decision

- 遠端只跑一個受限 container，不建 broker、relay 或 upload helper。Slack token 與公司 gateway
  key 都進同一個 container，由 OpenAB 依 `inherit_env` 傳給 agent process。
- OpenAB 唯一 runtime config 是 `config/openab.toml`，agent 使用 `opencode acp`。
- Claude ACP adapter 與 Claude Code CLI 仍以固定版本安裝在 image，僅作 OMO 可委派的 specialist，
  不作 OpenAB deployment runtime 或 rollback。
- 版本與 OpenAB immutable image digest 記在 `config/versions.env`；`compose.yaml` 使用 checked-in
  build args，因此 `docker compose` 可直接執行，不需要 `scripts/compose.sh`。
- OMO 使用 autonomous routing；wrapper 失敗時明確回報，不靜默 fallback。

## Superseded history

本 ADR 舊版曾描述第二份 OpenAB config、Claude ACP deployment rollback、runtime selector 與
`scripts/compose.sh` 強制入口；這些內容已失效，不可作為操作指示。現行正本是
`compose.yaml`、`config/openab.toml` 與 `config/versions.env`。

## Consequences

只有一個 container 和一份 OpenAB config 要建置、驗證與排障。代價是所有 runtime secret 共用一個
失效邊界；這是明確接受的取捨。Claude specialist 仍使用 `claude-credentials` named volume。
