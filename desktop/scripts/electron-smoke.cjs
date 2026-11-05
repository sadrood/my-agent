/** Electron 启动冒烟：拉起窗口、无 console error、截图。CI 用 xvfb 跑（无真实后端）。 */
const { _electron } = require('@playwright/test');

(async () => {
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  const app = await _electron.launch({ args: ['.'], cwd: process.cwd(), env });
  const errors = [];
  app.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  const win = await app.firstWindow();
  win.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await win.waitForTimeout(4500);
  const title = await win.title();
  await win.screenshot({ path: 'electron-smoke.png' });
  console.log('title:', title);
  console.log('console errors:', errors.slice(0, 10));
  await app.close();
  if (!title) throw new Error('窗口标题为空：Electron 没能正常拉起');
  // CI 上没有后端：WS 连不上 8090 是预期错误，不计入失败。
  const expected = /ERR_CONNECTION_REFUSED|Failed to load resource|net::ERR_|WebSocket connection to 'ws:\/\/.*\/ws' failed/;
  const unexpected = errors.filter((t) => !expected.test(t));
  if (unexpected.length) throw new Error(`渲染进程有 ${unexpected.length} 条 console error`);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
