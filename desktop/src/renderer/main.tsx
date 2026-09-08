import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { ErrorBoundary } from './components/ErrorBoundary';
import './styles/global.css';
// 代码高亮配色跟随主题：不再固定引入 github-dark.css（浅色主题下代码块一片深黑）。
// 配色改为在 global.css 里用 CSS 变量定义（浅/深两套，黑白极简风）。

// 主题属性提升到 <html>：body/html 必须吃到主题级联，否则 body 背景永远
// 回退 :root 的深色值（浅色主题下窗口两侧发黑的根因）。App 内主题切换时同步。
try {
  const saved = JSON.parse(localStorage.getItem('my-agent-ui') || '{}');
  document.documentElement.dataset.theme = saved?.state?.theme === 'light' ? 'light' : 'dark';
} catch {
  document.documentElement.dataset.theme = 'dark';
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
