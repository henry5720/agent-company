// 關掉 chrome sidecar default context 裡連續 IDLE_MS 沒換過網址的分頁。
//
// 由 scripts/reap-browser-tabs.sh 在 agent container 裡用 `node --input-type=module` 從 stdin 執行：
// container 跟 sidecar 共用 localhost，連得到 127.0.0.1:9222，也有 Node 22（內建 WebSocket）。
//
// 為什麼只看 default context：thread 照 agents/AGENTS.md 開 isolated context，那些由 playwright-cli
// 自己收（`close`，或閒置 PLAYWRIGHT_MCP_IDLE_TIMEOUT 後 detach，兩者都會關掉 context 與分頁）。
// default context 只剩沒照 SOP 開的分頁與 chrome-devtools MCP 的分頁，沒有人會回來收，只能猜：
// 每小時記一次網址，同一個分頁連續 IDLE_MS 網址都沒變就關。理由見 docs/adr/0016-reclaim-browser-tabs-by-idleness.md。
//
// REAP_DRY_RUN=1 時只印出會關哪些，不關、不寫 state。

export const decide = (state, pages, now, idleMs) => {
  const next = {};
  const close = [];
  for (const page of pages) {
    const prev = state[page.targetId];
    const since = prev && prev.url === page.url ? prev.since : now;
    if (now - since >= idleMs) close.push(page);
    else next[page.targetId] = { url: page.url, since };
  }
  return { next, close };
};

const main = async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const endpoint = process.env.CHROME_ENDPOINT ?? 'http://127.0.0.1:9222';
  const idleMs = Number(process.env.BROWSER_TAB_IDLE_MS ?? 4 * 60 * 60 * 1000);
  const stateFile =
    process.env.REAP_STATE ?? '/home/node/.cache/agent-company/browser-tabs.json';
  const dryRun = process.env.REAP_DRY_RUN === '1';

  const { webSocketDebuggerUrl } = await (await fetch(`${endpoint}/json/version`)).json();
  const ws = new WebSocket(webSocketDebuggerUrl);
  let seq = 0;
  const pending = new Map();
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    const settle = pending.get(msg.id);
    if (!settle) return;
    pending.delete(msg.id);
    if (msg.error) settle.reject(new Error(`${msg.error.message} (${msg.error.code})`));
    else settle.resolve(msg.result);
  };
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      pending.set(++seq, { resolve, reject });
      ws.send(JSON.stringify({ id: seq, method, params }));
    });
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error(`cannot open ${webSocketDebuggerUrl}`));
  });

  const { browserContextIds } = await send('Target.getBrowserContexts');
  const isolated = new Set(browserContextIds);
  const { targetInfos } = await send('Target.getTargets');
  const pages = targetInfos.filter(
    (t) => t.type === 'page' && !isolated.has(t.browserContextId)
  );

  let state = {};
  try {
    state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  } catch {
    // 第一次跑或 state 壞掉：全部從現在起算，這一輪不會關任何分頁。
  }
  const { next, close } = decide(state, pages, Date.now(), idleMs);

  for (const page of close) {
    if (!dryRun) await send('Target.closeTarget', { targetId: page.targetId });
    console.log(`${dryRun ? 'would close' : 'closed'}: ${page.url}`);
  }
  console.log(
    `default-context pages: ${pages.length}, kept ${Object.keys(next).length}, ` +
      `${dryRun ? 'would close' : 'closed'} ${close.length}; isolated contexts left alone: ${isolated.size}`
  );
  if (!dryRun) {
    fs.mkdirSync(path.dirname(stateFile), { recursive: true });
    fs.writeFileSync(stateFile, JSON.stringify(next));
  }
  ws.close();
};

if (process.env.REAP_BROWSER_TABS_TEST !== '1') await main();
