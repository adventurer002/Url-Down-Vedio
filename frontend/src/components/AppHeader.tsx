import { Link } from "react-router-dom";
import Icon from "./Icon";
import { useWorkspace } from "../hooks/useWorkspace";
export default function AppHeader() {
  const store = useWorkspace();
  return (
    <>
      <a href="#main" className="skip-link">
        跳转至主要内容
      </a>
      <header className="app-header">
        <Link to="/" className="brand" aria-label="VideoHub 首页">
          <span className="brand-icon">
            <Icon name="play" size={20} />
          </span>
          VIDEOHUB<span className="brand-ai">AI</span>
        </Link>
        <div className="header-right">
          <span className="header-note">从视频，到洞见。</span>
          <button
            className="history-button"
            onClick={() => store.setHistoryOpen(true)}
          >
            <Icon name="history" size={18} />
            任务历史
            {store.activeCount > 0 && (
              <span className="count">{store.activeCount}</span>
            )}
          </button>
        </div>
      </header>
    </>
  );
}
