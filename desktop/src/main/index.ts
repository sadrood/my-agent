/**
 * Electron 主进程
 *
 * 职责：
 * 1. 创建三栏桌面窗口（加载 Vite dev server 或打包后的静态文件）
 * 2. 自动拉起/连接 Python FastAPI 后端（dashboard.server，端口 8090）
 * 3. 通过 IPC 暴露后端控制能力（启动/停止/状态）给渲染进程
 */
import { app, BrowserWindow, ipcMain, shell, Menu, Notification, safeStorage, webContents, clipboard, dialog, screen } from 'electron';
import { spawn, exec, execSync, ChildProcess } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';
import * as http from 'http';
import type { WebContents } from 'electron';

const BACKEND_PORT = 8090;
const BACKEND_BASE = `http://127.0.0.1:${BACKEND_PORT}`;
/** 内嵌浏览器桥端口：Python Agent 经此控制侧栏 <webview>（仅监听 127.0.0.1） */
const BROWSER_BRIDGE_PORT = 8091;

let mainWindow: BrowserWindow | null = null;
/** 桌面宠物浮窗（透明无边框，独立 pet.html） */
let petWindow: BrowserWindow | null = null;
let petPanelWindow: BrowserWindow | null = null;
let backendProc: ChildProcess | null = null;
/** 是否主动停止后端（关窗口 / 手动重启）——此时退出不该自动拉起 */
let backendManualStop = false;
/** 意外退出的连续重试计数，就绪一次即清零 */
let backendCrashRetries = 0;
/** 当前生效的工作目录（多工作区：为空 = 项目根） */
let currentWorkDir = '';

/** 默认工作区：首次运行时创建在「文档/小悟工作区」（对用户可见、文件树好浏览），
 *  与安装目录、userData 均分离——发布版数据只落在这一处。 */
function defaultWorkspaceDir(): string {
  let docs = '';
  try { docs = app.getPath('documents'); } catch { docs = app.getPath('home'); }
  const dir = path.join(docs, '小悟工作区');
  try { fs.mkdirSync(dir, { recursive: true }); } catch { /* 失败则调用方回退 */ }
  return dir;
}

/** 工作目录持久化（userData/workspace.json）：重启应用后仍生效 */
function workspaceFile(): string {
  return path.join(app.getPath('userData'), 'workspace.json');
}

function loadWorkspace(): string {
  try {
    const d = JSON.parse(fs.readFileSync(workspaceFile(), 'utf-8'));
    return typeof d.workDir === 'string' ? d.workDir : '';
  } catch {
    return '';
  }
}

function saveWorkspace(dir: string): void {
  try {
    fs.mkdirSync(path.dirname(workspaceFile()), { recursive: true });
    fs.writeFileSync(workspaceFile(), JSON.stringify({ workDir: dir }, null, 2), 'utf-8');
  } catch {
    /* 持久化失败不阻塞（下次设置再写） */
  }
}

/** 项目根目录（desktop/ 的上一级） */
function projectRoot(): string {
  // 开发时 process.cwd() 是 desktop/；打包后 __dirname 在 dist/main
  const candidates = [
    path.resolve(process.cwd()),
    path.resolve(__dirname, '..', '..', '..'),
    path.resolve(__dirname, '..', '..'),
  ];
  for (const c of candidates) {
    if (fs.existsSync(path.join(c, 'main.py'))) return c;
  }
  return process.cwd();
}

/** 探测 8090 上是否已有本项目的 dashboard 后端（用 hub 状态快照的指纹字段判断） */
async function probeBackendOnPort(): Promise<boolean> {
  try {
    const res = await fetch(`${BACKEND_BASE}/api/state`);
    if (!res.ok) return false;
    const d = await res.json();
    return !!(d && typeof d === 'object' && 'subscriber_count' in d && 'total_events' in d);
  } catch {
    return false;
  }
}

/** 杀掉占用指定端口的残留后端进程（win32: netstat+taskkill；失败静默，不阻塞启动） */
function killPortOwner(port: number): Promise<void> {
  return new Promise((resolve) => {
    if (process.platform !== 'win32') { resolve(); return; }
    exec(
      `netstat -ano | findstr :${port} | findstr LISTENING`,
      { encoding: 'utf-8', windowsHide: true },
      (_err, stdout) => {
        const m = /LISTENING\s+(\d+)\s*$/m.exec(stdout.trim());
        if (!m) { resolve(); return; }
        exec(`taskkill /PID ${m[1]} /F`, { windowsHide: true }, () => resolve());
      },
    );
  });
}

/** 启动 Python 后端（复用 dashboard.server，换端口避免与已有 8080 冲突）。
 *  多工作区：设置了工作目录则后端以该目录为 cwd（WORKSPACE_ROOT=工作目录，
 *  文件树/回滚/会话均落在工作区内）；PYTHONPATH 指向项目根保证模块可导入。 */
async function startBackend(): Promise<void> {
  if (backendProc) return;
  backendManualStop = false;
  const root = projectRoot();
  const saved = loadWorkspace();
  let cwd = saved && fs.existsSync(saved) ? saved : defaultWorkspaceDir();
  currentWorkDir = cwd;
  try { fs.mkdirSync(cwd, { recursive: true }); } catch { /* 不可建则回退 root */ }
  if (!fs.existsSync(cwd)) { currentWorkDir = cwd = root; }
  const python = fs.existsSync(path.join(root, '.venv', 'Scripts', 'python.exe'))
    ? path.join(root, '.venv', 'Scripts', 'python.exe')
    : 'python';

  // 打包版（安装给最终用户）：后端是藏在 resources/backend 的单文件 exe，
  // 不需要项目源码与 Python；工作区已重定向到上面解析的 cwd（默认文档目录）。
  const packaged = app.isPackaged;
  const backendExe = packaged
    ? path.join(process.resourcesPath, 'backend', 'myagent-backend.exe')
    : '';
  if (packaged && !fs.existsSync(backendExe)) {
    console.warn('[main] 打包版缺少内置后端资源: ' + backendExe);
  }

  // 上次异常退出可能残留孤儿后端占着 8090（EADDRINUSE 会让新后端反复崩溃重试）。
  // 先探测：端口上是本项目后端 → 清理掉再拉起，保证每次拿到干净、配置正确的后端。
  if (await probeBackendOnPort()) {
    console.log('[main] 检测到 8090 残留后端，清理后重启');
    await killPortOwner(BACKEND_PORT);
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline && await probeBackendOnPort()) {
      await new Promise((r) => setTimeout(r, 150));
    }
  }

  const envBase: NodeJS.ProcessEnv = {
    ...process.env,
    MY_AGENT_DISABLE_QUICKEDIT: 'false',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    // 打包版/无 .env 环境也继承完全信任沙箱（computer 键盘鼠标级控制可用）
    SANDBOX_MODE: 'danger-full-access',
    // 内置终端桥：Agent 的 terminal 命令经此执行并回显到内置终端面板
    MY_AGENT_EMBEDDED_TERMINAL_URL: `http://127.0.0.1:${BROWSER_BRIDGE_PORT}/terminal`,
  };
  let cmd: string;
  let args: string[];
  if (packaged && fs.existsSync(backendExe)) {
    cmd = backendExe;
    args = [];
    console.log(`[main] 启动内置后端: ${backendExe} (cwd=${cwd})`);
  } else {
    cmd = python;
    args = ['-m', 'dashboard.server', '--port', String(BACKEND_PORT)];
    envBase.PYTHONPATH = process.env.PYTHONPATH ? `${root}${path.delimiter}${process.env.PYTHONPATH}` : root;
    console.log(`[main] 启动后端: ${cmd} ${args.join(' ')} (cwd=${cwd})`);
  }
  // 告知 Python 侧启用内嵌浏览器工具（browser 命令转发到侧栏 webview）
  envBase.MY_AGENT_EMBEDDED_BROWSER_URL = `http://127.0.0.1:${BROWSER_BRIDGE_PORT}/browser`;
  if (packaged) {
    // 打包后端：由环境变量指定端口并静音控制台日志；工作目录即工作区
    envBase.MY_AGENT_BACKEND_PORT = String(BACKEND_PORT);
    envBase.MY_AGENT_QUIET = '1';
  }
  backendProc = spawn(cmd, args, { cwd, stdio: 'pipe', env: envBase });
  const logPath = path.join(app.getPath('userData'), 'backend.log');
  const toLog = (d: Buffer | string) => {
    const line = d.toString();
    console.log(`[backend] ${line}`.trimEnd());
    try { fs.appendFileSync(logPath, line, 'utf-8'); } catch { /* 日志写入失败不影响运行 */ }
  };
  backendProc.stdout?.on('data', toLog);
  backendProc.stderr?.on('data', toLog);
  backendProc.on('exit', (code) => {
    console.log(`[main] 后端退出 code=${code}`);
    backendProc = null;
    // 意外崩溃（窗口还开着、非主动停止）→ 指数退避自动拉起，窗口关闭时才放弃
    if (!backendManualStop && mainWindow && backendCrashRetries < 5) {
      backendCrashRetries++;
      const delay = Math.min(1000 * 2 ** (backendCrashRetries - 1), 8000);
      console.log(`[main] ${delay}ms 后自动重启后端（第 ${backendCrashRetries} 次）`);
      setTimeout(() => { void startBackend(); }, delay);
    }
  });
  // 等待后端就绪（轮询 /api/state，最多 15s）
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${BACKEND_BASE}/api/state`);
      if (res.ok) {
        console.log('[main] 后端就绪');
        backendCrashRetries = 0;
        return;
      }
    } catch {
      /* 未就绪，重试 */
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  console.warn('[main] 后端 15s 内未就绪，继续启动窗口（前端会显示连接失败）');
}

function stopBackend(): void {
  backendManualStop = true;
  if (backendProc) {
    backendProc.kill();
    backendProc = null;
  }
}

// ---------------- 内嵌浏览器桥（Python Agent ⇄ 侧栏 <webview>） ----------------
// Python 端 EmbeddedBrowserTool 把 browser 命令 POST 到这里，
// 主进程找到对应标签页的 webContents 后用 executeJavaScript /
// sendInputEvent / capturePage 执行，返回 JSON 结果。
// 多标签：渲染层经 IPC webview:register 登记每个 tab 的 tabId→WebContents，
// 桥动作按 tab_id（缺省=active）定位。

const CHROME_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36';

const webviewRegistry = new Map<string, WebContents>();   // tabId -> WebContents
let activeWebviewTabId = '';
/** 当前桥动作要定位的标签（来自请求 payload 的 tab_id；空=用渲染层 active） */
let activeBridgeTabId = '';

type BridgeResult = { ok: boolean; output?: string; base64?: string; error?: string };

function findEmbeddedWebview(tabId?: string): WebContents | null {
  const pick = (id: string): WebContents | null => {
    const wc = webviewRegistry.get(id);
    if (wc && !wc.isDestroyed()) return wc;
    if (wc) webviewRegistry.delete(id);
    return null;
  };
  if (tabId && pick(tabId)) return pick(tabId);
  if (activeWebviewTabId && pick(activeWebviewTabId)) return pick(activeWebviewTabId);
  for (const wc of webContents.getAllWebContents()) {
    if (wc.getType() === 'webview' && !wc.isDestroyed()) return wc;
  }
  return null;
}

function allWebviews(): Array<{ tabId: string; url: string; title: string }> {
  const out: Array<{ tabId: string; url: string; title: string }> = [];
  for (const [tabId, wc] of webviewRegistry) {
    if (wc.isDestroyed()) continue;
    out.push({ tabId, url: wc.getURL(), title: wc.getTitle() });
  }
  return out;
}

/** 按 数字索引 / tabId / URL 片段 解析目标标签，返回 tabId（找不到返回空串） */
function resolveTabTarget(target: string): string {
  const tabs = allWebviews();
  if (/^\d+$/.test(target)) {
    const idx = parseInt(target, 10);
    if (idx >= 0 && idx < tabs.length) return tabs[idx].tabId;
    return '';
  }
  const hit = tabs.find((t) => t.tabId === target || t.url.includes(target));
  return hit ? hit.tabId : '';
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** 在侧栏 webview 里执行 JS（executeJavaScript 结果 JSON 字符串化后回传） */
async function evalInWebview(code: string): Promise<unknown> {
  const wc = findEmbeddedWebview(activeBridgeTabId);
  if (!wc) throw new Error('侧栏内嵌浏览器未打开');
  // 超时保护：executeJavaScript 在页面卡死（如 alert 弹窗阻塞）时会永久挂起，
  // 导致 Agent 操作内置浏览器"卡死"。8s 超时返回明确错误而非无限等待。
  const r = (await Promise.race([
    wc.executeJavaScript(code, true),
    new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error('页面无响应（executeJavaScript 超时 8s）')), 8000),
    ),
  ])) as unknown;
  if (typeof r === 'string') {
    try { return JSON.parse(r); } catch { return r; }
  }
  return r;
}

function normalizeUrl(u: string): string {
  const url = (u || '').trim();
  if (!url) return 'about:blank';
  if (/^https?:\/\//i.test(url)) return url;
  return 'https://' + url;
}

const KEY_CODES: Record<string, string> = {
  enter: 'Enter', esc: 'Escape', escape: 'Escape', tab: 'Tab', backspace: 'Backspace',
  delete: 'Delete', del: 'Delete', up: 'ArrowUp', down: 'ArrowDown',
  left: 'ArrowLeft', right: 'ArrowRight', space: 'Space', home: 'Home', end: 'End',
  pageup: 'PageUp', pagedown: 'PageDown',
};

async function handleBrowserAction(action: string, p: Record<string, unknown>): Promise<BridgeResult> {
  activeBridgeTabId = String(p.tab_id || '');
  switch (action) {
    case 'open': {
      const url = String(p.url || '').trim();
      // 让渲染层开新标签（或同 URL 已开则切过去）
      const sendOpen = () => mainWindow?.webContents.send('embedded-browser:open', { url: normalizeUrl(url) });
      sendOpen();
      const deadline = Date.now() + 9000;
      let lastResend = 0;
      while (Date.now() < deadline) {
        const wc = findEmbeddedWebview();
        if (wc) {
          const tabs = allWebviews();
          const title = wc.getTitle() || wc.getURL();
          return {
            ok: true,
            output: url
              ? `已打开「${title}」（当前 ${tabs.length} 个标签页）。`
              : `内嵌浏览器已在运行中（当前 ${tabs.length} 个标签页）。`,
          };
        }
        // 事件可能丢失：3s/6s 各重发一次（同 URL 已开会自动切换，不会重复建标签）
        if (Date.now() - lastResend > 3000 && Date.now() - deadline < 7000) {
          lastResend = Date.now();
          sendOpen();
        }
        await sleep(200);
      }
      return { ok: false, error: '侧栏 webview 未创建（渲染层未响应 browser:open）' };
    }
    case 'tabs': {
      const tabs = allWebviews();
      if (!tabs.length) return { ok: true, output: '没有已打开的标签页。发送 browser open <url> 打开。' };
      const lines = tabs.map((t, i) => {
        const marker = t.tabId === activeWebviewTabId ? '●' : '○';
        return `${marker} [${i}] ${t.title || '(无标题)'}  ${t.url}`;
      });
      return { ok: true, output: `已打开 ${tabs.length} 个标签页（[索引] 标题 URL）：\n` + lines.join('\n') };
    }
    case 'switch': {
      const target = String(p.tab ?? p.index ?? '').trim();
      const tabId = resolveTabTarget(target);
      if (!tabId) return { ok: false, error: `找不到标签: '${target}'` };
      mainWindow?.webContents.send('embedded-browser:switch', { tab: tabId });
      await sleep(200);
      return { ok: true, output: '已切换到标签页。' };
    }
    case 'close': {
      const target = String(p.tab ?? p.index ?? '').trim();
      const tabId = resolveTabTarget(target);
      if (!tabId) return { ok: false, error: `找不到标签: '${target}'` };
      mainWindow?.webContents.send('embedded-browser:close-tab', { tab: tabId });
      await sleep(150);
      return { ok: true, output: '标签页已关闭。' };
    }
    case 'navigate': {
      const url = normalizeUrl(String(p.url || ''));
      let wc = findEmbeddedWebview();
      if (!wc && url && url !== 'about:blank') {
        // Agent 直接 navigate：自动让渲染层开标签（无需用户先手动打开网站）
        mainWindow?.webContents.send('embedded-browser:open', { url });
        const dl = Date.now() + 8000;
        while (Date.now() < dl && !wc) {
          await sleep(250);
          wc = findEmbeddedWebview();
        }
        if (!wc) return { ok: false, error: '浏览器标签未能自动创建（渲染层未响应）；可先手动在侧栏浏览器开一个标签后重试' };
      }
      if (!wc) return { ok: false, error: '侧栏内嵌浏览器未打开（请先 browser open <url>）' };
      // ERR_ABORTED 是假失败：目标站重定向/替换导航会让 loadURL 的 Promise 以
      // ERR_ABORTED 拒绝，但页面实际会加载成功。不能因此判死——等页面落定再按真实状态回报。
      let navErr = '';
      try {
        await wc.loadURL(url);
      } catch (e) {
        navErr = String((e as Error)?.message || e);
      }
      // 最多等 8s 让重定向链落定（每 250ms 查一次）
      const dl = Date.now() + 8000;
      let settled = false;
      while (Date.now() < dl) {
        let cur = '';
        let loading = true;
        try { cur = wc.getURL() || ''; } catch { /* */ }
        try { loading = wc.isLoading(); } catch { /* */ }
        if (!loading && cur && !cur.startsWith('about:')) { settled = true; break; }
        await sleep(250);
      }
      let cur = '';
      let title = '';
      try { cur = wc.getURL() || ''; } catch { /* */ }
      try { title = wc.getTitle() || ''; } catch { /* */ }
      if (settled || (cur && !cur.startsWith('about:'))) {
        const redirectNote = cur !== url ? `（最终地址: ${cur}）` : '';
        return { ok: true, output: `已导航到: ${url}${redirectNote}
页面标题: ${title || '(无标题)'}` };
      }
      return { ok: false, error: navErr ? `导航失败: ${navErr.slice(0, 200)}` : `导航超时：${url}` };
    }
    case 'back':
      return { ok: true, output: (await evalInWebview('history.back(); "ok"') as string) ? '已返回上一页。' : '' };
    case 'forward':
      await evalInWebview('history.forward()');
      return { ok: true, output: '已前进。' };
    case 'reload':
      await evalInWebview('location.reload()');
      return { ok: true, output: '页面已刷新。' };
    case 'title': {
      const wc = findEmbeddedWebview();
      if (!wc) return { ok: false, error: '侧栏内嵌浏览器未打开' };
      return { ok: true, output: `页面标题: ${wc.getTitle()}` };
    }
    case 'url': {
      const wc = findEmbeddedWebview();
      if (!wc) return { ok: false, error: '侧栏内嵌浏览器未打开' };
      return { ok: true, output: `当前 URL: ${wc.getURL()}` };
    }
    case 'text': {
      const r = await evalInWebview(
        'JSON.stringify({ t: document.body ? document.body.innerText : "" })',
      ) as { t?: string };
      let text = r?.t || '';
      if (text.length > 5000) text = text.slice(0, 5000) + '\n\n... (文本过长，已截断。)';
      return { ok: true, output: text };
    }
    case 'html': {
      const r = await evalInWebview(
        'JSON.stringify({ h: document.documentElement ? document.documentElement.outerHTML : "" })',
      ) as { h?: string };
      let html = r?.h || '';
      if (html.length > 8000) html = html.slice(0, 8000) + `\n\n... (HTML 过长，已截断。总长度: ${html.length} 字符)`;
      return { ok: true, output: html };
    }
    case 'snapshot': {
      const r = await evalInWebview(`(() => {
        const els = document.querySelectorAll('a,button,[role=button],input,select,textarea,h1,h2,h3,[onclick]');
        const lines = [];
        for (const e of Array.from(els).slice(0, 120)) {
          const tag = e.tagName.toLowerCase();
          const info = ((e.innerText || e.placeholder || e.value || e.getAttribute('aria-label') || '') + '').trim().replace(/\\s+/g, ' ').slice(0, 60);
          const sel = e.id ? '#' + e.id : '';
          lines.push('- ' + tag + sel + ' "' + info + '"');
        }
        return JSON.stringify({ lines });
      })()`) as { lines?: string[] };
      const lines = r?.lines || [];
      if (!lines.length) return { ok: true, output: '（页面无可见交互元素，可改用 text/html 命令了解内容）' };
      const wc = findEmbeddedWebview();
      return { ok: true, output: `[页面元素快照] ${wc ? wc.getURL() : ''}\n` + lines.join('\n') };
    }
    case 'click': {
      const r = await evalInWebview(`(() => {
        const sel = ${JSON.stringify(String(p.selector || ''))};
        let el = null;
        try { el = document.querySelector(sel); } catch (e) { el = null; }
        if (!el) {
          for (const c of document.querySelectorAll('a,button,[role=button],input[type=submit],input[type=button],label,span,div,li')) {
            const t = ((c.innerText || c.value || c.placeholder || '') + '').trim();
            if (t && t.includes(sel) && t.length < 80) { el = c; break; }
          }
        }
        if (!el) return JSON.stringify({ found: false });
        el.scrollIntoView({ block: 'center' });
        el.click();
        return JSON.stringify({ found: true, text: ((el.innerText || el.value || '') + '').trim().slice(0, 80) });
      })()`) as { found?: boolean; text?: string };
      if (!r?.found) return { ok: false, error: `无法找到或点击元素: '${String(p.selector || '')}'` };
      await sleep(300);
      return { ok: true, output: `已点击元素: ${String(p.selector || '')}${r.text ? `（${r.text}）` : ''}` };
    }
    case 'type': {
      const r = await evalInWebview(`(() => {
        const sel = ${JSON.stringify(String(p.selector || ''))};
        const text = ${JSON.stringify(String(p.text ?? ''))};
        let el = null;
        try { el = document.querySelector(sel); } catch (e) {}
        if (!el) el = document.activeElement;
        if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA' && !el.isContentEditable)) return JSON.stringify({ found: false });
        el.focus();
        if (el.isContentEditable) {
          el.textContent = text;
          el.dispatchEvent(new InputEvent('input', { bubbles: true }));
        } else {
          const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
          if (setter) setter.call(el, text);
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        }
        return JSON.stringify({ found: true });
      })()`) as { found?: boolean };
      if (!r?.found) return { ok: false, error: `无法找到输入框: '${String(p.selector || '')}'` };
      return { ok: true, output: `已输入文本。` };
    }
    case 'press': {
      const wc = findEmbeddedWebview();
      if (!wc) return { ok: false, error: '侧栏内嵌浏览器未打开' };
      const key = String(p.key || '').trim();
      const code = KEY_CODES[key.toLowerCase()] || (key.length === 1 ? key.toUpperCase() : '');
      if (!code) return { ok: false, error: `不支持的按键: '${key}'` };
      wc.sendInputEvent({ type: 'keyDown', keyCode: code });
      wc.sendInputEvent({ type: 'keyUp', keyCode: code });
      return { ok: true, output: `已按下按键: ${key}` };
    }
    case 'scroll': {
      await evalInWebview(`window.scrollBy(${Number(p.dx || 0)}, ${Number(p.dy || 0)}); "ok"`);
      return { ok: true, output: '页面已滚动。' };
    }
    case 'js': {
      const r = await evalInWebview(String(p.code || ''));
      const out = typeof r === 'string' ? r : JSON.stringify(r);
      return { ok: true, output: `JS 执行结果: ${(out || '(无返回值)').slice(0, 500)}` };
    }
    case 'screenshot': {
      const wc = findEmbeddedWebview();
      if (!wc) return { ok: false, error: '侧栏内嵌浏览器未打开' };
      const img = await wc.capturePage();
      return { ok: true, base64: img.toJPEG(85).toString('base64') };
    }
    case 'status': {
      const wc = findEmbeddedWebview();
      if (!wc) return { ok: true, output: '内嵌浏览器未打开（侧栏「浏览器」标签页未开启）。发送 launch 打开。' };
      return {
        ok: true,
        output: `浏览器状态: 运行中（侧栏内嵌）\n当前 URL: ${wc.getURL()}\n页面标题: ${wc.getTitle()}`,
      };
    }
    case 'clickat': case 'dblclickat': case 'mousemove': case 'mousescroll': {
      const wc = findEmbeddedWebview();
      if (!wc) return { ok: false, error: '侧栏内嵌浏览器未打开' };
      const x = Number(p.x || 0); const y = Number(p.y || 0);
      if (action === 'mousemove') {
        wc.sendInputEvent({ type: 'mouseMove', x, y });
        return { ok: true, output: `鼠标已移动到 (${x}, ${y})` };
      }
      if (action === 'mousescroll') {
        wc.sendInputEvent({ type: 'mouseWheel', x, y, deltaX: Number(p.dx || 0), deltaY: Number(p.dy || 0) });
        return { ok: true, output: '已滚动。' };
      }
      const btn = (String(p.button || 'left') as 'left' | 'right' | 'middle');
      const count = action === 'dblclickat' ? 2 : 1;
      wc.sendInputEvent({ type: 'mouseDown', x, y, button: btn, clickCount: count });
      wc.sendInputEvent({ type: 'mouseUp', x, y, button: btn, clickCount: count });
      return { ok: true, output: action === 'dblclickat' ? `已双击 (${x}, ${y})` : `已点击 (${x}, ${y})` };
    }
    case 'keycombo': {
      const wc = findEmbeddedWebview();
      if (!wc) return { ok: false, error: '侧栏内嵌浏览器未打开' };
      const parts = String(p.keys || '').split('+').map((s) => s.trim()).filter(Boolean);
      const mods: string[] = [];
      let main = '';
      for (const part of parts) {
        const l = part.toLowerCase();
        if (l === 'control' || l === 'ctrl') mods.push('control');
        else if (l === 'shift') mods.push('shift');
        else if (l === 'alt') mods.push('alt');
        else if (l === 'meta' || l === 'cmd' || l === 'super') mods.push('super');
        else main = part;
      }
      const code = KEY_CODES[main.toLowerCase()] || (main.length === 1 ? main.toUpperCase() : '');
      if (!code) return { ok: false, error: `无法解析组合键: '${String(p.keys || '')}'` };
      wc.sendInputEvent({ type: 'keyDown', keyCode: code, modifiers: mods as never });
      wc.sendInputEvent({ type: 'keyUp', keyCode: code, modifiers: mods as never });
      return { ok: true, output: `已发送组合键: ${String(p.keys || '')}` };
    }
    case 'type-direct': {
      const wc = findEmbeddedWebview();
      if (!wc) return { ok: false, error: '侧栏内嵌浏览器未打开' };
      wc.insertText(String(p.text ?? ''));
      return { ok: true, output: '已直接输入文本到焦点元素。' };
    }
    default:
      return { ok: false, error: `内嵌浏览器不支持的动作: ${action}` };
  }
}

// ================= 内置终端（桥接回显式） =================
// Agent（Python 桥）与用户共用同一条命令流：命令在 Electron 侧执行，
// 结果回显到内置终端面板；面板新挂载时经 IPC terminal:snapshot 取历史。

interface TermEntry { ts: number; from: 'user' | 'agent'; command: string; ok: boolean; output: string }

const termBuffer: TermEntry[] = [];
const TERM_CAP = 200;

function sendTermEcho(e: TermEntry): void {
  mainWindow?.webContents.send('terminal:echo', e);
}

function runTermCommand(command: string, from: 'user' | 'agent'): Promise<TermEntry> {
  return new Promise((resolve) => {
    const child = process.platform === 'win32'
      // chcp 65001：cmd 输出统一 UTF-8，回显不再按 GBK 乱码
      ? spawn('cmd', ['/d', '/s', '/c', `chcp 65001>nul& ${command}`], { windowsHide: true, env: process.env })
      : spawn('sh', ['-c', command], { env: process.env });
    let out = '';
    let done = false;
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* 已退出 */ }
      finish(false, `命令超时（>300s），已终止。\n${out.slice(-2000)}`);
    }, 300_000);
    const finish = (ok: boolean, forceOut?: string) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      const entry: TermEntry = {
        ts: Date.now(), from,
        command: command.slice(0, 500),
        ok,
        output: (forceOut !== undefined ? forceOut : out).slice(0, 8000),
      };
      termBuffer.push(entry);
      if (termBuffer.length > TERM_CAP) termBuffer.splice(0, termBuffer.length - TERM_CAP);
      sendTermEcho(entry);
      // Agent 首次跑命令时自动把右侧面板切到「终端」并固定导航栏
      if (from === 'agent') mainWindow?.webContents.send('terminal:open');
      resolve(entry);
    };
    child.stdout?.on('data', (d: Buffer | string) => { out += String(d); });
    child.stderr?.on('data', (d: Buffer | string) => { out += String(d); });
    child.on('error', (e) => finish(false, `启动失败: ${e.message}`));
    child.on('close', (code) => finish(code === 0));
  });
}

function startBrowserBridge(): void {
  const server = http.createServer((req, res) => {
    if (req.method === 'GET' && (req.url || '').startsWith('/health')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, bridge: 'embedded-browser' }));
      return;
    }
    // 内置终端桥：{action:'run', command} → 执行并回显到面板
    if (req.method === 'POST' && (req.url || '').startsWith('/terminal')) {
      const chunks: Buffer[] = [];
      req.on('data', (c) => chunks.push(c));
      req.on('end', () => {
        let payload: Record<string, unknown> = {};
        try { payload = JSON.parse(Buffer.concat(chunks).toString('utf-8') || '{}'); } catch { /* 空 */ }
        const command = String(payload.command || '');
        if (!command.trim()) {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: false, error: '命令为空' }));
          return;
        }
        void runTermCommand(command, 'agent').then((e) => {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: e.ok, output: e.output }));
        });
      });
      return;
    }
    if (req.method !== 'POST' || !(req.url || '').startsWith('/browser')) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'not found' }));
      return;
    }
    const chunks: Buffer[] = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      let payload: Record<string, unknown> = {};
      try { payload = JSON.parse(Buffer.concat(chunks).toString('utf-8') || '{}'); } catch { /* 保持空 */ }
      const action = String(payload.action || '');
      void handleBrowserAction(action, payload)
        .then((result) => {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify(result));
        })
        .catch((e: unknown) => {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: false, error: `内嵌浏览器执行失败: ${String((e as Error)?.message || e).slice(0, 200)}` }));
        });
    });
  });
  // 仅监听本机回环：桥不对局域网暴露
  server.listen(BROWSER_BRIDGE_PORT, '127.0.0.1', () => {
    console.log(`[main] 内嵌浏览器桥: http://127.0.0.1:${BROWSER_BRIDGE_PORT}/browser`);
  });
  server.on('error', (e) => console.warn(`[main] 内嵌浏览器桥启动失败（桌面端 Agent 将回退独立浏览器）: ${e.message}`));
}

// Agent 显示名（设置里改名字 → IPC app:set-title 更新；窗口标题/通知/桌宠标题跟随）
let appDisplayName = '小悟';

// ==================== Git 更新（源码/git 安装版自更新） ====================
// 逻辑：git fetch → 落后判定 → git pull → npm run build（重编译）→ relaunch。
// 打包 exe（无 .git）走提示，不用本通道。
function repoRoot(): string {
  // 本文件位于 desktop/dist/main → 上溯三级 = 项目根（my_agent）
  return path.resolve(__dirname, '..', '..', '..');
}
function runGit(args: string[], cwd: string, timeoutMs = 90000): Promise<{ code: number; out: string }> {
  return new Promise((resolve) => {
    let proxyVal = '';
    try {
      proxyVal = execSync('git config --get http.proxy', { cwd: repoRoot(), timeout: 8000, encoding: 'utf-8' }).trim();
    } catch { /* 仓库没配代理时直连 */ }
    const gitArgs = proxyVal ? ['-c', `http.proxy=${proxyVal}`, ...args] : args;
    const child = spawn('git', gitArgs, {
      cwd,
      windowsHide: true,
      env: { ...process.env, GIT_TERMINAL_PROMPT: '0' },
    });
    let out = '';
    child.stdout?.on('data', (d: Buffer | string) => { out += String(d); });
    child.stderr?.on('data', (d: Buffer | string) => { out += String(d); });
    const timer = setTimeout(() => { try { child.kill(); } catch { /* */ } }, timeoutMs);
    child.on('error', (e) => { clearTimeout(timer); resolve({ code: -1, out: e.message }); });
    child.on('close', (code) => { clearTimeout(timer); resolve({ code: code ?? -1, out: out.trim() }); });
  });
}
async function gitUpdateState(): Promise<Record<string, unknown>> {
  const root = repoRoot();
  if (!fs.existsSync(path.join(root, '.git'))) {
    return { git: false, reason: '非 git 安装（打包版请用新安装包/整包更新）' };
  }
  const branch = (await runGit(['rev-parse', '--abbrev-ref', 'HEAD'], root)).out.trim() || 'main';
  await runGit(['fetch', 'origin'], root);
  const cur = (await runGit(['rev-parse', '--short', 'HEAD'], root)).out.trim();
  const remote = `origin/${branch}`;
  const behindRaw = (await runGit(['rev-list', '--count', `HEAD..${remote}`], root)).out.trim();
  const aheadRaw = (await runGit(['rev-list', '--count', `${remote}..HEAD`], root)).out.trim();
  const behind = parseInt(behindRaw || '0', 10) || 0;
  const ahead = parseInt(aheadRaw || '0', 10) || 0;
  const latest = (await runGit(['rev-parse', '--short', remote], root)).out.trim();
  return { git: true, branch, current: cur, latest, behind, ahead };
}
async function gitUpdateNow(): Promise<Record<string, unknown>> {
  const root = repoRoot();
  const branch = (await runGit(['rev-parse', '--abbrev-ref', 'HEAD'], root)).out.trim() || 'main';
  let pull = await runGit(['pull', '--ff-only', 'origin', branch], root, 180000);
  if (pull.code !== 0) {
    // 一次自动重试（网络/代理瞬断很常见）
    await new Promise((r) => setTimeout(r, 1200));
    pull = await runGit(['pull', '--ff-only', 'origin', branch], root, 180000);
  }
  if (pull.code !== 0) return { ok: false, error: `git pull 失败：${pull.out.slice(-300)}` };
  // 新代码已落盘 → 重编译 renderer/main/preload（dev/git 安装的自更新路径）
  const desktop = path.join(root, 'desktop');
  await new Promise<void>((resolve) => {
    const child = spawn('npm', ['run', 'build'], { cwd: desktop, shell: true, windowsHide: true, env: process.env });
    child.on('close', () => resolve());
    child.on('error', () => resolve());
  });
  const head = (await runGit(['rev-parse', '--short', 'HEAD'], root)).out.trim();
  return { ok: true, head };
}
ipcMain.handle('app:update-check', () => gitUpdateState());
ipcMain.handle('app:update-now', async () => {
  const r = await gitUpdateNow();
  if (r.ok) {
    // 去掉 dev server 指向，重启后加载刚编译的 dist 资源
    setTimeout(() => {
      process.env.VITE_DEV_SERVER_URL = '';
      app.relaunch();
      app.exit(0);
    }, 800);
  }
  return r;
});

ipcMain.handle('app:set-title', (_e, name: string) => {
  const v = String(name || '').trim();
  if (v) appDisplayName = v;
  if (mainWindow) mainWindow.setTitle(`${appDisplayName} Desktop`);
  return true;
});

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 600,
    title: `${appDisplayName} Desktop`,
    backgroundColor: '#0f1115',
    // 自定义标题栏：隐藏原生标题条，Windows 用原生 overlay 窗口按钮（缩放/关闭），
    // 拖拽区由状态栏承担（CSS -webkit-app-region）
    titleBarStyle: 'hidden',
    titleBarOverlay: process.platform === 'win32'
      ? { color: '#1b1b1a', symbolColor: '#aaa69c', height: 46 }
      : undefined,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // 侧栏内嵌浏览器（<webview> 标签）：Agent 经 HTTP 桥控制它，用户所见即 Agent 所控
      webviewTag: true,
    },
  });

  const devUrl = process.env.VITE_DEV_SERVER_URL;
  if (devUrl) {
    mainWindow.loadURL(devUrl);
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));
  }

  // 外链用系统浏览器打开
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // 无应用菜单栏后，保留 F12 / Ctrl+Shift+I 开发者工具快捷键
  mainWindow.webContents.on('before-input-event', (_event, input) => {
    if (input.type === 'keyDown' && (input.key === 'F12' || (input.control && input.shift && input.key.toLowerCase() === 'i'))) {
      mainWindow?.webContents.toggleDevTools();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
    stopBackend();
  });
}

// ---------------- 桌面宠物浮窗 ----------------
// 透明无边框小窗（pet.html，纯 vanilla）：呼吸圆球 + 状态气泡 + 拖拽 +
// 点击聚焦主窗。状态经 'pet:status' IPC 由主窗口推送。

function createPetWindow(): void {
  if (petWindow && !petWindow.isDestroyed()) {
    petWindow.show();
    return;
  }
  const wa = screen.getPrimaryDisplay().workArea;
  petWindow = new BrowserWindow({
    width: 150,
    height: 214,
    x: wa.x + wa.width - 168,
    y: wa.y + wa.height - 234,
    frame: false,
    transparent: true,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    title: appDisplayName,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'index.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  petWindow.setAlwaysOnTop(true, 'screen-saver');
  const devUrl = process.env.VITE_DEV_SERVER_URL;
  if (devUrl) {
    void petWindow.loadURL(`${devUrl.replace(/\/$/, '')}/pet.html`);
  } else {
    void petWindow.loadFile(path.join(__dirname, '..', 'renderer', 'pet.html'));
  }
  petWindow.on('closed', () => { petWindow = null; });
}

// 单实例锁：防止多个桌面端实例同时跑、互相清理对方后端（启动时 kill 孤儿逻辑的前提）
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

app.whenReady().then(async () => {
  if (!gotLock) return;
  // 隐藏默认英文菜单栏（干净的桌面客户端外观）
  Menu.setApplicationMenu(null);
  startBrowserBridge();
  await startBackend();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

// ---------------- IPC ----------------
ipcMain.handle('backend:status', async () => {
  try {
    const res = await fetch(`${BACKEND_BASE}/api/state`);
    return res.ok ? { running: true } : { running: false };
  } catch {
    return { running: false };
  }
});

ipcMain.handle('backend:restart', async (_event, workDir?: string) => {
  if (typeof workDir === 'string' && workDir.trim() && fs.existsSync(workDir.trim())) {
    saveWorkspace(workDir.trim());
  }
  stopBackend();
  await startBackend();
  return { ok: true };
});

ipcMain.handle('app:get-backend-base', () => BACKEND_BASE);

ipcMain.handle('app:get-version', () => app.getVersion());

// ---- 内置终端 IPC（面板读取历史 / 用户执行命令）----
ipcMain.handle('terminal:snapshot', () => termBuffer);
ipcMain.handle('terminal:user-run', async (_e, command: string) => {
  const c = String(command || '').trim();
  if (!c) return { ok: false, error: '命令为空' };
  const e = await runTermCommand(c, 'user');
  return { ok: e.ok, output: e.output };
});

// 剪贴板兜底通道：渲染层 navigator.clipboard 偶发被焦点/权限拒绝时使用
ipcMain.handle('clipboard:write', (_event, text: string) => {
  clipboard.writeText(String(text ?? ''));
  return { ok: true };
});

// 当前工作目录（状态栏显示用）
ipcMain.handle('app:get-workdir', () => currentWorkDir);

// 原生目录选择器（项目添加/切换用）：弹系统文件夹选择框，返回路径或空串
ipcMain.handle('app:pick-directory', async () => {
  try {
    const res = await dialog.showOpenDialog({
      title: '选择项目目录',
      properties: ['openDirectory', 'createDirectory'],
    });
    if (!res.canceled && res.filePaths.length > 0) {
      return { ok: true, path: res.filePaths[0] };
    }
    return { ok: false, path: '' };
  } catch {
    return { ok: false, path: '' };
  }
});

// ---------------- 内嵌浏览器多标签：渲染层登记 tabId↔WebContents ----------------
ipcMain.handle('webview:register', (_e, tabId: string, wcId: number) => {
  const wc = webContents.fromId(wcId);
  if (wc && tabId) webviewRegistry.set(tabId, wc);
  return { ok: true };
});
ipcMain.handle('webview:unregister', (_e, tabId: string) => {
  webviewRegistry.delete(tabId);
  if (activeWebviewTabId === tabId) activeWebviewTabId = '';
  return { ok: true };
});
ipcMain.handle('webview:set-active', (_e, tabId: string) => {
  activeWebviewTabId = tabId;
  return { ok: true };
});

// 主题切换时同步 Windows 标题栏 overlay 颜色（非 Windows / 不支持时静默跳过）
ipcMain.handle('app:set-titlebar-overlay', (_event, theme: string) => {
  if (process.platform !== 'win32' || !mainWindow) return;
  try {
    mainWindow.setTitleBarOverlay(theme === 'light'
      ? { color: '#f6f3ed', symbolColor: '#6f695e' }
      : { color: '#1a1a19', symbolColor: '#aaa69c' });
  } catch {
    /* 旧版本 Windows 不支持 overlay 时忽略 */
  }
});

// 任务完成提醒：窗口不在前台时任务栏闪烁 + 弹系统通知（点击通知聚焦窗口）
ipcMain.handle('app:notify-done', (_event, status?: string) => {
  if (!mainWindow) return;
  if (mainWindow.isFocused()) return;
  mainWindow.flashFrame(true);
  const body = status === 'failed' ? '❌ 任务失败'
    : status === 'stopped' ? '⏹ 任务已停止'
      : status === 'completed' ? '✅ 任务完成'
        : '任务已结束';
  if (Notification.isSupported()) {
    const n = new Notification({ title: `${appDisplayName} Desktop`, body, silent: false });
    n.on('click', () => {
      if (mainWindow) {
        if (mainWindow.isMinimized()) mainWindow.restore();
        mainWindow.show();
        mainWindow.focus();
      }
    });
    n.show();
  }
});

// ---------------- 密钥安全存储（safeStorage/DPAPI 加密落盘） ----------------
// 渲染层 localStorage 不再保存明文 key：密文存 userData/secure.json，解密只在主进程。

// ---------------- 桌面宠物 IPC ----------------
ipcMain.handle('pet:toggle', (_event, show?: boolean) => {
  const visible = typeof show === 'boolean' ? show : !(petWindow && petWindow.isVisible());
  if (visible) createPetWindow();
  else petWindow?.hide();
  return { visible };
});
ipcMain.handle('pet:status', (_event, running: boolean, endStatus?: string) => {
  if (petWindow && !petWindow.isDestroyed()) {
    petWindow.webContents.send('pet:status', Boolean(running), endStatus || '');
  }
  return { ok: true };
});
ipcMain.handle('pet:focus-main', () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  }
  return { ok: true };
});
// 宠物拖拽：增量移动窗口位置（JS 拖拽替代 -webkit-app-region，
// 因为 drag 区域会吞掉点击事件，导致宠物点不出菜单）
ipcMain.handle('pet:move', (_event, dx: number, dy: number) => {
  if (petWindow && !petWindow.isDestroyed()) {
    const [x, y] = petWindow.getPosition();
    petWindow.setPosition(Math.round(x + (Number(dx) || 0)), Math.round(y + (Number(dy) || 0)));
  }
  return { ok: true };
});
// 宠物菜单动作 → 主窗口渲染层（新对话 / 暂停任务）
ipcMain.handle('pet:action', (_event, action: string) => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('pet:action', String(action || ''));
  }
  if (action === 'new-session' || action === 'stop-run') {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
    }
  }
  return { ok: true };
});

// ---- 桌宠对话面板（点宠物开面板，历史+收发消息） ----

function createPetPanelWindow(): void {
  if (petPanelWindow && !petPanelWindow.isDestroyed()) {
    petPanelWindow.show();
    petPanelWindow.focus();
    return;
  }
  petPanelWindow = new BrowserWindow({
    width: 360,
    height: 540,
    frame: false,
    transparent: true,
    resizable: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: true,
    title: `${appDisplayName} · 对话`,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'index.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  petPanelWindow.setAlwaysOnTop(true, 'screen-saver');
  const devUrl = process.env.VITE_DEV_SERVER_URL;
  if (devUrl) {
    void petPanelWindow.loadURL(`${devUrl.replace(/\/$/, '')}/pet-panel.html`);
  } else {
    void petPanelWindow.loadFile(path.join(__dirname, '..', 'renderer', 'pet-panel.html'));
  }
  petPanelWindow.on('closed', () => { petPanelWindow = null; });
}

function sendPetPush(payload: unknown): void {
  const msg = JSON.stringify(payload ?? {});
  for (const w of [petWindow, petPanelWindow]) {
    if (w && !w.isDestroyed()) w.webContents.send('pet:push', msg);
  }
}

ipcMain.handle('pet:open-panel', () => { createPetPanelWindow(); return { ok: true }; });

// 面板/宠物发送消息 → 主窗口渲染层执行（真实走 sendGoal）
ipcMain.handle('pet:send-message', (_event, text: string, sessionId?: string | null) => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('pet:chat', {
      text: String(text || '').slice(0, 4000),
      sessionId: sessionId ? String(sessionId) : null,
    });
  }
  return { ok: true };
});

// 主窗口渲染层 → 宠物两窗（状态/摘要推送）
ipcMain.on('pet:push', (_event, payload) => { sendPetPush(payload); });


function secureFile(): string {
  return path.join(app.getPath('userData'), 'secure.json');
}

function readSecure(): Record<string, string> {
  try {
    return JSON.parse(fs.readFileSync(secureFile(), 'utf-8'));
  } catch {
    return {};
  }
}

function writeSecure(data: Record<string, string>): void {
  try {
    fs.mkdirSync(path.dirname(secureFile()), { recursive: true });
    fs.writeFileSync(secureFile(), JSON.stringify(data), 'utf-8');
  } catch {
    /* 落盘失败下次再写 */
  }
}

ipcMain.handle('secure:set', (_event, name: string, value: string) => {
  if (!name) return { ok: false, error: '缺少 name' };
  if (!safeStorage.isEncryptionAvailable()) return { ok: false, error: '系统加密不可用' };
  const data = readSecure();
  if (!value) {
    delete data[name];
    writeSecure(data);
    return { ok: true };
  }
  data[name] = safeStorage.encryptString(value).toString('base64');
  writeSecure(data);
  return { ok: true };
});

ipcMain.handle('secure:get', (_event, name: string) => {
  const encrypted = readSecure()[name];
  if (!encrypted) return { ok: true, value: '' };
  try {
    return { ok: true, value: safeStorage.decryptString(Buffer.from(encrypted, 'base64')) };
  } catch {
    return { ok: false, error: '解密失败（系统或用户变更？）' };
  }
});
