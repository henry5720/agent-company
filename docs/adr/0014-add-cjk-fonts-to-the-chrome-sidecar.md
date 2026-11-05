# Chrome sidecar 加一層中文字型

## Status

Accepted（2026-11-05）。補 [0012](0012-follow-the-official-openab-codex-deployment.md)。

## Background

`chrome` sidecar 原本直接跑 `chromedp/headless-shell`。這個 image 只帶 Chrome 本身，
連 `fc-list` 都沒有，也沒有中文字型。2026-11-05 Slack 上 bot 開繁中網站給人登入時，
畫面的中文全是方框。

字型只能裝在 sidecar：渲染網頁的是 sidecar 裡的 Chrome，agent container 裡有什麼字型它都用不到。
agent 也裝不了：它以 `node` 身分執行，沒有 root。

## Decision

新增 `chrome/Dockerfile`：`FROM` 原本固定 digest 的 headless-shell，只加裝 Debian 的
`fonts-noto-cjk`，套件版本固定。base 與字型版本的正本都在 `config/versions.env`，
`compose.yaml` 用 build args 傳入，`tests/static.sh` 斷言兩邊一致。entrypoint、資源上限、
不放 token、不掛 volume 都不變。

沒選的做法：

- **mount host 的字型目錄**：sidecar 會依賴 deployment host 裝了什麼，換機器就壞。
- **改用自帶字型的 Chrome image**：要重新評估 entrypoint 與監聽介面；headless-shell 已經驗過
  只聽 127.0.0.1 的設定。
- **一起裝 emoji 字型**：`fonts-noto-color-emoji` 的版本帶 `+deb13uN`，Debian 出安全更新後
  舊版會從套件庫消失，固定版本的 build 會壞。等實際缺 emoji 再加。

## Consequences

- `scripts/deploy.sh` 的 `compose build --pull` 會一起 build sidecar；build 需要連得到 Debian 套件庫。
- 升級 headless-shell 時，若 base 的 Debian release 變了，要用 `apt-cache policy fonts-noto-cjk`
  重查並更新 `FONTS_NOTO_CJK_VERSION`，否則 build 失敗。這是刻意的。
- sidecar image 從 pull 變成 build，`agent-company-chrome:<Chrome 版本>` 是本機產物，不推到 registry。
