import { Link, useNavigate } from "react-router-dom";

import { useMe } from "../hooks/useVideos";
import { useAuthStore } from "../stores/auth";

export function Logo() {
  return (
    <Link to="/" className="flex items-center gap-2">
      <span className="text-lg leading-none text-brand" aria-hidden>
        ▼
      </span>
      <span className="text-sm font-medium tracking-tight">down-vedio</span>
    </Link>
  );
}

export function Nav() {
  const loggedIn = useAuthStore((s) => !!s.accessToken);
  const clear = useAuthStore((s) => s.clear);
  const navigate = useNavigate();
  const { data: me } = useMe(loggedIn);

  return (
    <header className="border-b border-line">
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-6">
        <Logo />
        <nav className="flex items-center gap-5 text-sm">
          <Link to="/" className="text-muted transition-colors hover:text-ink">
            首页
          </Link>
          <Link to="/plans" className="text-muted transition-colors hover:text-ink">
            套餐
          </Link>
          {loggedIn ? (
            <>
              <Link to="/history" className="text-muted transition-colors hover:text-ink">
                历史记录
              </Link>
              {me?.user.is_admin && (
                <Link to="/admin" className="text-muted transition-colors hover:text-ink">
                  管理
                </Link>
              )}
              <span className="hidden meta sm:inline">
                {me ? `${me.plan_name} · 今日 ${me.today_downloads_used}/${me.today_downloads_quota}` : ""}
              </span>
              <button
                className="text-muted transition-colors hover:text-ink"
                onClick={() => {
                  clear();
                  navigate("/");
                }}
              >
                退出
              </button>
            </>
          ) : (
            <>
              <Link to="/login" className="text-muted transition-colors hover:text-ink">
                登录
              </Link>
              <Link to="/register" className="btn btn-primary btn-sm">
                注册
              </Link>
            </>
          )}
        </nav>
      </div>
    </header>
  );
}

export function Footer() {
  return (
    <footer className="border-t border-line">
      <div className="mx-auto flex h-14 max-w-7xl items-center justify-between px-6 text-xs text-muted">
        <span className="font-mono">down-vedio</span>
        <span>视频解析 · 下载 · AI 处理</span>
      </div>
    </footer>
  );
}
