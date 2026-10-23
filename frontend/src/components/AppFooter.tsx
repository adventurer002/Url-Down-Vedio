import { Link } from "react-router-dom";
export default function AppFooter() {
  return (
    <footer className="app-footer">
      <span className="footer-identity">
        <strong>
          VIDEOHUB <span className="footer-ai">AI</span>
        </strong>
        <span className="footer-divider">|</span>
        <span>让内容成为你的知识。</span>
      </span>
      <nav className="footer-links" aria-label="帮助与偏好设置">
        <Link to="/help">使用帮助</Link>
        <Link to="/privacy">隐私说明</Link>
        <Link to="/settings">设置</Link>
      </nav>
    </footer>
  );
}
