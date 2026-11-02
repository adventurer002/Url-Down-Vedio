import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import {
  QueryCache,
  QueryClient,
  QueryClientProvider,
  useIsFetching,
  useQueryClient,
} from "@tanstack/react-query";
import * as Dialog from "@radix-ui/react-dialog";
import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { authStore, useAuth } from "../stores/auth";
import { pageLabel } from "../utils/navigation";
import { normalizeError } from "../api/client";
import Icon from "../components/Icon";
import WeChatLoginModal from "../components/auth/WeChatLoginModal";
import AuditPage from "../admin/AuditPage";
import StoragePage from "../admin/StoragePage";
import UsersPage from "../admin/UsersPage";
import UserDetailPage from "../admin/UserDetail";
import AllowancesDetailPage from "../admin/AllowancesPage";
import TasksPage from "../admin/TasksPage";
import TaskDetailPage from "../admin/TaskDetail";
import {
  OperationNotice,
  OperationProvider,
  adminAuthFailure,
} from "../admin/operations";
import OverviewPage, { adminKeys } from "../admin/OverviewPage";
import { formatTime, Loading } from "../admin/ui";
import type { Overview } from "../admin/api";
import "../admin/admin.css";
import "../admin/readability.css";

function Gate({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="admin-app admin-gate">
      <div className="admin-gate-card">
        <span className="admin-gate-icon">
          <Icon name="shield" size={28} />
        </span>
        <p className="admin-eyebrow">VIDEOHUB AI / 管理控制台</p>
        <h1>{title}</h1>
        {children}
        <Link to="/" reloadDocument className="admin-text-button">
          返回网站 <Icon name="arrow" size={16} />
        </Link>
      </div>
    </div>
  );
}
function Sidebar({ close }: { close?: () => void }) {
  const auth = useAuth();
  return (
    <>
      <Link className="admin-brand" to="/admin" onClick={close}>
        <span className="admin-logo">
          <Icon name="play" size={19} />
        </span>
        <span>
          <strong>
            VIDEOHUB <b>AI</b>
          </strong>
          <small>管理控制台</small>
        </span>
      </Link>
      <nav className="admin-nav" aria-label="管理导航">
        <p>工作空间</p>
        {[
          ["dashboard", "概览", "/admin"],
          ["text", "任务管理", "/admin/tasks"],
          ["user", "用户管理", "/admin/users"],
          ["shield", "会员与额度", "/admin/allowances"],
        ].map(([icon, label, path]) => (
          <NavLink
            key={path}
            to={path!}
            end={path === "/admin"}
            onClick={close}
            className={({ isActive }) => (isActive ? "admin-nav-active" : "")}
          >
            <Icon name={icon} size={18} />
            {label}
          </NavLink>
        ))}
        <div className="admin-nav-divider" />
        <p>系统管理</p>
        {[
          ["history", "操作审计", "/admin/audit"],
          ["server", "存储与服务", "/admin/system"],
        ].map(([icon, label, path]) => (
          <NavLink
            key={path}
            to={path!}
            onClick={close}
            className={({ isActive }) => (isActive ? "admin-nav-active" : "")}
          >
            <Icon name={icon} size={18} />
            {label}
          </NavLink>
        ))}
      </nav>
      <div className="admin-sidebar-bottom">
        <Link to="/" reloadDocument onClick={close}>
          <Icon name="external" size={18} />
          返回网站
        </Link>
        <div className="admin-identity">
          <span className="admin-avatar">
            <Icon name="user" size={20} />
          </span>
          <div>
            <strong>{auth.user?.nickname}</strong>
            <small>管理员</small>
          </div>
        </div>
      </div>
    </>
  );
}
function AccountMenu() {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function logout() {
    setBusy(true);
    setError("");
    try {
      await authStore.logout();
    } catch (cause) {
      setError(normalizeError(cause).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Dropdown.Root>
        <Dropdown.Trigger
          className="admin-icon-button admin-avatar"
          aria-label="管理员账户菜单"
        >
          <Icon name="user" size={18} />
        </Dropdown.Trigger>
        <Dropdown.Portal>
          <Dropdown.Content
            className="admin-dropdown"
            align="end"
            sideOffset={8}
          >
            <Dropdown.Label>管理员账户</Dropdown.Label>
            <Dropdown.Item asChild>
              <Link to="/" reloadDocument>返回网站</Link>
            </Dropdown.Item>
            <Dropdown.Separator />
            <Dropdown.Item disabled={busy} onSelect={() => void logout()}>
              {busy ? "正在退出…" : "退出登录"}
            </Dropdown.Item>
          </Dropdown.Content>
        </Dropdown.Portal>
      </Dropdown.Root>
      {error && (
        <p className="admin-logout-error" role="alert">
          {error}
          <button onClick={() => void logout()} disabled={busy}>
            重试退出
          </button>
        </p>
      )}
    </>
  );
}
function AdminLayout() {
  const client = useQueryClient();
  const fetching = useIsFetching({ queryKey: ["admin"] });
  const data = client.getQueryData<Overview>(adminKeys.overview);
  const [menuOpen, setMenuOpen] = useState(false);
  const { pathname } = useLocation();
  useEffect(() => {
    if (window.scrollY > 0) window.scrollTo({ top: 0, behavior: "auto" });
    document.getElementById("admin-main")?.focus({ preventScroll: true });
  }, [pathname]);
  const overview = pathname === "/admin" || pathname === "/admin/";
  const pageLabel = overview
    ? "概览"
    : pathname.startsWith("/admin/tasks")
      ? "任务管理"
      : pathname.startsWith("/admin/users")
        ? "用户管理"
        : pathname.startsWith("/admin/allowances")
          ? "会员与额度"
          : pathname.startsWith("/admin/audit")
            ? "操作审计"
            : pathname.startsWith("/admin/system")
              ? "存储与服务"
              : "页面未找到";
  return (
    <div className="admin-app">
      <a href="#admin-main" className="skip-link">
        跳转至主要内容
      </a>
      <aside className="admin-sidebar">
        <Sidebar />
      </aside>
      <div className="admin-workspace">
        <header className="admin-topbar">
          <div className="admin-breadcrumb">
            <Dialog.Root open={menuOpen} onOpenChange={setMenuOpen}>
              <Dialog.Trigger
                className="admin-icon-button admin-mobile-menu"
                aria-label="打开管理导航"
              >
                <Icon name="menu" />
              </Dialog.Trigger>
              <Dialog.Portal>
                <Dialog.Overlay className="admin-overlay" />
                <Dialog.Content className="admin-mobile-sidebar">
                  <Dialog.Title className="admin-sr-only">
                    管理导航
                  </Dialog.Title>
                  <Dialog.Description className="admin-sr-only">
                    管理控制台导航菜单
                  </Dialog.Description>
                  <Sidebar close={() => setMenuOpen(false)} />
                  <Dialog.Close
                    className="admin-icon-button admin-menu-close"
                    aria-label="关闭管理导航"
                  >
                    <Icon name="close" />
                  </Dialog.Close>
                </Dialog.Content>
              </Dialog.Portal>
            </Dialog.Root>
            <span>控制台</span>
            <span>/</span>
            <strong>{pageLabel}</strong>
          </div>
          <div className="admin-top-actions">
            {fetching > 0 && (
              <span role="status" className="admin-fetch-status">
                正在加载后台数据…
              </span>
            )}
            <span className="admin-updated">
              {overview ? `统计时间 ${formatTime(data?.as_of)}` : "管理工作台"}
            </span>
            <button
              className={`admin-icon-button${fetching > 0 ? " admin-refreshing" : ""}`}
              disabled={fetching > 0}
              aria-busy={fetching > 0}
              aria-label="刷新后台数据"
              onClick={() =>
                void client.invalidateQueries({ queryKey: ["admin"] })
              }
            >
              <Icon name="refresh" size={18} />
            </button>
            <span className="admin-top-divider" />
            <AccountMenu />
          </div>
        </header>
        <main id="admin-main" className="admin-content" tabIndex={-1}>
          <OperationNotice />
          <Routes>
            <Route path="/admin/audit" element={<AuditPage />} />
            <Route path="/admin/system" element={<StoragePage />} />
            <Route path="/admin" element={<OverviewPage />} />
            <Route path="/admin/users" element={<UsersPage />} />
            <Route path="/admin/users/:id" element={<UserDetailPage />} />
            <Route
              path="/admin/allowances"
              element={<UsersPage allowances />}
            />
            <Route
              path="/admin/allowances/:id"
              element={<AllowancesDetailPage />}
            />
            <Route path="/admin/tasks" element={<TasksPage />} />
            <Route path="/admin/tasks/:id" element={<TaskDetailPage />} />
            <Route
              path="*"
              element={
                <Gate title="页面未找到">
                  <p>这个管理页面尚未开放。</p>
                  <Link className="admin-button" to="/admin">
                    返回概览
                  </Link>
                </Gate>
              }
            />
          </Routes>
        </main>
        <footer className="admin-footer">
          <span>
            <strong>VIDEOHUB AI</strong> 管理控制台
          </span>
          <span>北京时间 · 按页面需要刷新</span>
        </footer>
      </div>
    </div>
  );
}
// A provider belongs to exactly one authenticated session. Disposal cancels its
// requests before clearing the cache; late responses cannot enter a new session.
function AdminSession() {
  const [denied, setDenied] = useState(false);
  const [client] = useState(
    () =>
      new QueryClient({
        queryCache: new QueryCache({
          onError: (error) => {
            if (adminAuthFailure(error)) setDenied(true);
          },
        }),
        defaultOptions: {
          queries: {
            staleTime: 15000,
            retry: false,
            refetchIntervalInBackground: false,
          },
        },
      }),
  );
  useEffect(
    () => () => {
      client.clear();
    },
    [client],
  );
  useEffect(() => {
    if (denied) client.clear();
  }, [client, denied]);
  if (denied)
    return (
      <Gate title="管理权限需要重新确认">
        <p>服务已拒绝访问，已停止读取管理数据。</p>
        <button
          className="admin-button admin-button-primary"
          onClick={() => void authStore.restore()}
        >
          重新验证身份
        </button>
      </Gate>
    );
  return (
    <QueryClientProvider client={client}>
      <OperationProvider onDenied={() => setDenied(true)}>
        <AdminLayout />
      </OperationProvider>
    </QueryClientProvider>
  );
}
export default function AdminView() {
  const auth = useAuth();
  const { pathname } = useLocation();
  useEffect(() => {
    const previous = document.title;
    document.title = `VideoHub AI · ${pageLabel(pathname)}`;
    return () => {
      document.title = previous;
    };
  }, [pathname]);
  if (auth.restoring)
    return (
      <Gate title="正在验证管理员身份">
        <Loading label="正在恢复登录状态…" />
      </Gate>
    );
  if (auth.restoreError)
    return (
      <Gate title="暂时无法验证身份">
        <p role="alert">{auth.restoreError}</p>
        <button
          className="admin-button admin-button-primary"
          onClick={() => void authStore.restore()}
        >
          重试身份验证
        </button>
      </Gate>
    );
  if (!auth.user || auth.user.role !== "admin" || auth.user.status !== "active")
    return (
      <>
        <Gate title={auth.user ? "暂无管理权限" : "登录管理控制台"}>
          <p>此页面仅供管理员访问。</p>
          <p>
            {auth.user
              ? "当前账号没有可用的管理员权限。"
              : "请使用已有管理员账号，通过微信登录继续。"}
          </p>
          {!auth.user && (
            <button
              className="admin-button admin-button-primary"
              onClick={authStore.open}
            >
              微信登录
            </button>
          )}
        </Gate>
        {auth.modalOpen && <WeChatLoginModal loadHistory={false} />}
      </>
    );
  return (
    <>
      <AdminSession key={`${auth.user.id}:${auth.token}`} />
      {auth.modalOpen && <WeChatLoginModal loadHistory={false} />}
    </>
  );
}
