import { Link } from "react-router-dom";
import { useAuth } from "../stores/auth";
export default function SettingsView() {
  const { user } = useAuth();
  return (
    <div className="preferences-page">
      <Link className="back-link" to="/app">
        ← 返回工作台
      </Link>
      <h1>设置</h1>
      <p className="preferences-description">查看文件保留与工作台设置。</p>
      <section className="preferences-card">
        <h2>文件保留</h2>
        <p>
          文件由平台统一管理，过期文件会按保留策略清理。有效结果和运行中的任务受保护。
        </p>
        {user?.role === "admin" && user.status === "active" ? (
          <Link className="back-link" to="/admin/system">
            前往存储与服务 →
          </Link>
        ) : (
          <p>存储维护由管理员负责。</p>
        )}
      </section>
    </div>
  );
}
