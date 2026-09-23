import { Link, useLocation, useNavigate } from "react-router-dom";

import JellyRadio from "./JellyRadio";
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
  const location = useLocation();
  const { data: me } = useMe(loggedIn);

  // 根据登录状态构建导航项
  const navItems = loggedIn
    ? [
        { value: "/", label: "首页" },
        { value: "/plans", label: "套餐" },
        { value: "/history", label: "历史" },
        ...(me?.user.is_admin
          ? [{ value: "/admin", label: "管理" }]
          : []),
      ]
    : [
        { value: "/", label: "首页" },
        { value: "/plans", label: "套餐" },
        { value: "/login", label: "登录" },
        { value: "/register", label: "注册" },
      ];

  // 当前路径对应的项目
  const currentPath =
    navItems.find((it) => it.value === location.pathname)?.value ??
    "/";

  return (
    <header className="border-b border-line">
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-6">
        <Logo />

        <div className="flex items-center gap-5">
          <JellyRadio
            items={navItems}
            value={currentPath}
            onChange={(val) => navigate(val)}
            chipColor="#eaeaea"
            activeColor="#171717"
            textColor="#666666"
            activeTextColor="#ffffff"
            size="sm"
            gap={6}
            radius={14}
            swell={0.15}
            barge={4}
            shrink={0.05}
            jelly={0.8}
            bounce={0.2}
            stagger={14}
            stiffness={580}
            ariaLabel="导航"
          />

          {loggedIn ? (
            <>
              <span className="hidden text-sm text-muted sm:inline">
                {me
                  ? `${me.plan_name} · 今日 ${me.today_downloads_used}/${me.today_downloads_quota}`
                  : ""}
              </span>
              <button
                className="text-base text-muted transition-colors hover:text-ink"
                onClick={() => {
                  clear();
                  navigate("/");
                }}
              >
                退出
              </button>
            </>
          ) : null}
        </div>
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
