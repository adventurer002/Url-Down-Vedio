import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useAuth, authStore } from "../stores/auth";
import { normalizeError } from "../api/client";
import WeChatLoginModal from "./auth/WeChatLoginModal";
import Icon from "./Icon";
import StarBorder from "./StarBorder";
import { useWorkspace } from "../hooks/useWorkspace";
export default function AppHeader() {
  const store = useWorkspace();
  const auth = useAuth();
  const { pathname } = useLocation();
  const [open, setOpen] = useState(false);
  const [logoutError, setLogoutError] = useState("");
  const [loggingOut, setLoggingOut] = useState(false);
  const menu = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    setOpen(false);
  }, [pathname, auth.user?.id]);
  useEffect(() => {
    if (!open) return;
    function outside(event: PointerEvent) {
      if (!menu.current?.contains(event.target as Node)) setOpen(false);
    }
    function escape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    }
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  async function logout() {
    if (loggingOut) return;
    setLoggingOut(true);
    try {
      await authStore.logout();
      setOpen(false);
      setLogoutError("");
    } catch (error) {
      setLogoutError(normalizeError(error).message);
    } finally {
      setLoggingOut(false);
    }
  }
  return (
    <>
      <a href="#main" className="skip-link">
        跳转至主要内容
      </a>
      <header
        className={
          "app-header" +
          (pathname.startsWith("/video/") ? " workspace-header" : "")
        }
      >
        <Link to="/" className="brand" aria-label="VideoHub 首页">
          <span className="brand-icon">
            <Icon name="play" size={20} />
          </span>
          VIDEOHUB<span className="brand-ai">AI</span>
        </Link>
        <div className="header-right">
          <button
            className="history-button"
            onClick={() => {
              if (authStore.requireLogin()) store.setHistoryOpen(true);
            }}
          >
            <Icon name="history" size={18} />
            任务历史
            {store.activeCount > 0 && (
              <span className="count">{store.activeCount}</span>
            )}
          </button>
          {auth.user && authStore.hasAccess() ? (
            <div className="account-menu" ref={menu}>
              <button
                ref={trigger}
                className="avatar-button"
                aria-label="账户菜单"
                aria-expanded={open}
                aria-controls="account-options"
                onClick={() => setOpen((value) => !value)}
              >
                <Icon name="user" size={20} />
              </button>
              {open && (
                <div id="account-options" className="account-popover">
                  <div className="account-identity">
                    <strong>{auth.user.nickname}</strong>
                    <span className="subscribed-badge">已关注</span>
                  </div>
                  <nav aria-label="账户选项">
                    <Link to="/settings">设置</Link>
                    <Link to="/help">使用帮助</Link>
                    {auth.user.role === "admin" && (
                      <Link to="/admin">管理界面</Link>
                    )}
                  </nav>
                  <button disabled={loggingOut} onClick={() => void logout()}>
                    {loggingOut ? "正在退出…" : "退出登录"}
                  </button>
                </div>
              )}
            </div>
          ) : (
            <StarBorder
              className="wechat-login-trigger star-login"
              onClick={authStore.open}
              theme="light"
              color="#18181b"
              trailColor="#18181b"
              duration={4}
              radius={10}
              thickness={1}
              glow={0.6}
              hover="lap"
              sparkle={false}
              clickPulse
            >
              登录
            </StarBorder>
          )}
        </div>
      </header>
      {(auth.message || logoutError) && (
        <p role="alert" className="auth-notice">
          {auth.message || logoutError}
        </p>
      )}
      {auth.modalOpen && <WeChatLoginModal />}
    </>
  );
}
