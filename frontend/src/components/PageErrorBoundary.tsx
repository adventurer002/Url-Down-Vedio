import { Component, type ReactNode } from "react";

export default class PageErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="missing-workspace" role="alert">
        <h1>页面暂时无法打开</h1>
        <p>可能是网络中断或页面已更新。刷新后重试，已提交的任务会继续执行。</p>
        <button
          className="button primary"
          onClick={() => window.location.reload()}
        >
          刷新页面
        </button>
        <a className="button secondary" href="/">
          返回首页
        </a>
      </div>
    );
  }
}
