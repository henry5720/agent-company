# repo 的 code 在不帶 token 的 prototype sandbox 跑

## Status

Accepted（已決定）。Amends [0003](0003-deploy-directly-on-the-fedora-host.md)、
[0004](0004-schedule-snapshots-with-cron.md)。來源：spec #21 §2、§6、§8。

## Context

[0012](0012-shared-workspace-agent-for-pms.md) 讓 PM 在 thread workspace 改 repo 做 prototype。
要看到結果就得 install、起 dev server、截圖，也就是執行 repo 裡任何人寫的 script。bot container
帶著 Slack bot token，又是所有 thread 共用的地方，在那裡跑 repo 的 code 等於把 token 交給
repo 的每一個依賴。

## Decision

- install、dev server／Storybook、Playwright 截圖、附件轉換都在 **prototype sandbox** 跑：
  Compose 常駐 2 格（`prototype-sandbox-1`、`prototype-sandbox-2`），各跑一個小 HTTP runner
  （`sandbox/runner`）。bot 只經 `sandbox-run` 打 runner port，指令不設限。
- sandbox **沒有任何 token**：沒有 `env_file`、不掛 Docker socket、不 privileged、以 node 身分跑。
  唯讀掛整個 `runtime/work`，每次執行先把原始碼複製（不帶 `.git`）到自己的 scratch 再跑；只寫
  `runtime/out/<key>/`。sandbox 若能寫 bot 看得到的 code 就能種 `.git/hooks`，所以 work 一定唯讀。
- 網路：bot 與 sandbox 同一個 network（`work-agent-net`，`enable_icc=false`），host 防火牆
  （`scripts/host-firewall.sh`，systemd oneshot unit 開機重下）讓 sandbox 只連得到外網（擋 host
  proxy、私有 IP 段、bot、EC2 IMDS），bot 只連得到 sandbox 的 runner port。IMDS 另以 hop limit 1
  當第二層。
- 只做 dev server／Storybook，不做 production build；repo 自己的 e2e 不支援。每格 5g、2 CPU。
- 「bot 不跑 install」只是 `agents/CLAUDE.md` 的約定，不是技術邊界：bot 有 shell，硬要跑擋不住。
- 第一階段只交付截圖與 HTML prototype，**不開預覽網址**。

## Consequences

- deployment host 需要 **root** 才能管理防火牆規則（`scripts/install-host-network.sh install`
  要 sudo）。deployment 帳號已有 sudo，所以 0003 的「沒有 sudo」前提不再成立；snapshot root 仍在
  維護者 home。0004 的 cron 照用，換回 systemd timer 不在這次範圍內。
- `br_netfilter` 載入後不可卸載：`modprobe -r br_netfilter` 會連 `bridge` 一起拿掉，實測讓正式
  bot 斷線約 7.5 小時。
- sandbox 磁碟不設 quota（overlay2 quota 要 xfs pquota，這台不符合）；靠 host cron 清 out
  （24 小時）與每週 `pnpm store prune`。
