/**
 * 全局错误边界：渲染异常不再白屏，显示局部错误与「重试/重载」入口。
 */
import React from 'react';

interface State {
  error: Error | null;
}

export class ErrorBoundary extends React.Component<{ children: React.ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[ErrorBoundary]', error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div style={{
        height: '100%', display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center', gap: 10,
        background: 'var(--bg-app)', color: 'var(--text-primary)', fontSize: 13,
        padding: 24, textAlign: 'center',
      }}>
        <div style={{ fontSize: 15, fontWeight: 600 }}>界面出了点问题</div>
        <div style={{ color: 'var(--text-muted)', maxWidth: 480, wordBreak: 'break-word' }}>
          {String(this.state.error?.message || this.state.error)}
        </div>
        <button
          className="btn primary"
          onClick={() => { this.setState({ error: null }); }}
        >
          重试渲染
        </button>
        <button className="btn" onClick={() => window.location.reload()}>重新加载页面</button>
      </div>
    );
  }
}
