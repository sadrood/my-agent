import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import * as path from 'path';
import { createHash } from 'crypto';


/** 生产构建注入 CSP：渲染进程只允许同源脚本、本机后端与内联样式。
 *  内联脚本（宠物窗/侧栏是 vanilla 页面）按内容算 sha256 精确放行，不放开 unsafe-inline。
 *  dev 不注入（Vite HMR 会插内联脚本，加了反而打断开发）。 */
const CSP_BASE = [
  "default-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "media-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:* http://localhost:* ws://localhost:*",
  "frame-src 'self' http: https:",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
];

function inlineScriptHashes(html: string): string[] {
  const hashes: string[] = [];
  const re = /<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html)) !== null) {
    const code = m[1];
    if (!code.trim()) continue;
    hashes.push(`'sha256-${createHash('sha256').update(code, 'utf8').digest('base64')}'`);
  }
  return hashes;
}

function cspPlugin() {
  return {
    name: "inject-csp",
    apply: "build" as const,
    transformIndexHtml(html: string) {
      const policy = [...CSP_BASE, `script-src 'self' ${inlineScriptHashes(html).join(" ")}`.trim()].join("; ");
      return html.replace(
        "<head>",
        `<head>
    <meta http-equiv="Content-Security-Policy" content="${policy}" />`,
      );
    },
  };
}

export default defineConfig({
  root: __dirname,
  plugins: [react(), tailwindcss(), cspPlugin()],
  base: './',
  server: {
    port: 5173,
    strictPort: true,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  build: {
    outDir: 'dist/renderer',
    emptyOutDir: true,
    rollupOptions: {
      // 多入口：主窗口 index.html + 桌面宠物 pet.html（独立透明小窗，纯 vanilla）
      input: {
        main: path.resolve(__dirname, 'index.html'),
        pet: path.resolve(__dirname, 'pet.html'),
        'pet-panel': path.resolve(__dirname, 'pet-panel.html'),
      },
      output: {
        // 把体积大头拆成独立 chunk：主包改动时 markdown/highlight 等大依赖不跟着重新下载
        manualChunks: {
          react: ['react', 'react-dom'],
          markdown: ['react-markdown', 'remark-gfm', 'rehype-highlight', 'highlight.js'],
        },
      },
    },
  },
});
