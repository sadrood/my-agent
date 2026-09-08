/**
 * 统一剪贴板写入：
 * 1. normalize：折叠 3+ 连续换行（模型回答/markdown 里的多余空行，渲染时不可见、
 *    粘贴出来却是两行空白）、清行尾空白与末尾换行；
 * 2. 写入：优先 navigator.clipboard（保留系统复制语义），被焦点/权限拒绝时
 *    自动兜底走 Electron 主进程 clipboard 模块（无任何限制）。
 */

/** 复制文本规范化（导出供测试） */
export function normalizeCopiedText(text: string): string {
  return text
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trimEnd();
}

/** 写剪贴板 + 多级兜底。normalize=false 用于代码/文件等需逐字精确的内容。
 * 1. navigator.clipboard（保留系统复制语义）
 * 2. Electron 主进程 clipboard 模块（窗口失焦 / 权限拒绝时）
 * 3. 临时 textarea + execCommand('copy')（无 preload / 纯浏览器环境也能用）
 */
export async function copyTextSmart(
  text: string,
  opts: { normalize?: boolean } = {},
): Promise<boolean> {
  const normalized = opts.normalize === false ? text : normalizeCopiedText(text);
  try {
    await navigator.clipboard.writeText(normalized);
    return true;
  } catch {
    // 第 2 级：Electron 主进程兜底
    try {
      if (window.desktopApi?.copyText) {
        await window.desktopApi.copyText(normalized);
        return true;
      }
    } catch { /* 继续降级 */ }
    // 第 3 级：临时 textarea + execCommand（同步，旧式但通用）
    try {
      const ta = document.createElement('textarea');
      ta.value = normalized;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }
}
