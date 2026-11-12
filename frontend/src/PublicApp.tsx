import { authStore, useAuth } from "./stores/auth";
import { lazy, Suspense, useEffect, useState } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { UploadTray } from "./components/UploadStatus";
import AppHeader from "./components/AppHeader";
import AppFooter from "./components/AppFooter";
import Icon from "./components/Icon";
import TaskHistory from "./components/TaskHistory";
import HomeView from "./views/HomeView";
import { useWorkspaceStore } from "./hooks/useWorkspace";
import { billingStore } from "./stores/billing";
import PageErrorBoundary from "./components/PageErrorBoundary";
import { useBillingRefresh } from "./hooks/useBillingRefresh";
import { pageLabel } from "./utils/navigation";
import PageReady from "./components/PageReady";
import { metadata, siteOrigin } from "./marketing/content";
import { cleanPath } from "./marketing/routes";
const LibraryView = lazy(() => import("./views/LibraryView"));
const SubscriptionView = lazy(() => import("./views/SubscriptionView"));
const AccountView = lazy(() => import("./views/AccountView"));
const VideoWorkspaceView = lazy(() => import("./views/VideoWorkspaceView"));

export default function PublicApp() {
  const store = useWorkspaceStore();
  const auth = useAuth();
  const [authNoticeDismissed, setAuthNoticeDismissed] = useState(false);
  useEffect(() => {
    if (!auth.restoreError) setAuthNoticeDismissed(false);
  }, [auth.restoreError]);
  useBillingRefresh();
  useEffect(() => {
    if (auth.restoring || auth.restoreError) return;
    store.switchUser(auth.user?.id || null);
    billingStore.setUser(auth.user?.id || null);
    if (auth.user && authStore.hasAccess())
      void store.loadHistory().catch(() => {});
  }, [
    store,
    auth.user?.id,
    auth.user?.status,
    auth.user?.is_subscribed,
    auth.restoring,
    auth.restoreError,
  ]);
  const { pathname } = useLocation();
  const label = pageLabel(pathname);
  useEffect(() => {
    const subscription = cleanPath(pathname) === "/subscription";
    const info = subscription ? metadata("/subscription") : null;
    document.title = info?.title || `VideoHub AI · ${label}`;
    let robots = document.querySelector<HTMLMetaElement>('meta[name="robots"]');
    if (!robots) {
      robots = document.createElement("meta");
      robots.name = "robots";
      document.head.appendChild(robots);
    }
    robots.content =
      subscription &&
      import.meta.env.PROD &&
      import.meta.env.VITE_PUBLIC_INDEXING !== "false"
        ? "index,follow"
        : "noindex,follow";
    const description = document.querySelector<HTMLMetaElement>(
      'meta[name="description"]',
    );
    if (description)
      description.content =
        info?.description ||
        "在 VideoHub AI 工作台查看你的音视频、文稿与处理结果。";
    if (subscription) {
      let canonical = document.querySelector<HTMLLinkElement>(
        'link[rel="canonical"]',
      );
      if (!canonical) {
        canonical = document.createElement("link");
        canonical.rel = "canonical";
        document.head.appendChild(canonical);
      }
      canonical.href = `${siteOrigin}/subscription`;
    } else {
      document.querySelector('link[rel="canonical"]')?.remove();
      document
        .querySelectorAll('script[type="application/ld+json"]')
        .forEach((item) => item.remove());
      document
        .querySelectorAll('meta[property^="og:"], meta[name^="twitter:"]')
        .forEach((item) => item.remove());
    }
  }, [label, pathname]);
  useEffect(() => {
    if (auth.restoring || auth.restoreError) return;
    store.restore();
    window.addEventListener("pagehide", store.flushPersistence);
    return () => {
      window.removeEventListener("pagehide", store.flushPersistence);
      store.stopAll();
    };
  }, [store, auth.restoring, auth.restoreError]);
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [pathname]);
  return (
    <div className="public-app">
      <AppHeader />
      <UploadTray />
      {auth.restoreError &&
        !authNoticeDismissed &&
        cleanPath(pathname) !== "/subscription" && (
          <div className="auth-notice auth-notice-floating" role="alert">
            <Icon name="warning" size={21} />
            <div className="auth-notice-content">
              <strong>登录状态暂时无法恢复</strong>
              <p>{auth.restoreError}</p>
            </div>
            <button
              className="text-button"
              type="button"
              onClick={() => void authStore.restore()}
            >
              重新连接
            </button>
            <button
              className="auth-notice-close"
              type="button"
              aria-label="关闭登录状态提醒"
              title="关闭提醒"
              onClick={() => setAuthNoticeDismissed(true)}
            >
              <Icon name="close" size={18} />
            </button>
          </div>
        )}
      <main id="main" tabIndex={-1}>
        <PageErrorBoundary key={pathname}>
          <Suspense
            fallback={
              <p className="muted" role="status">
                正在打开{label}…
              </p>
            }
          >
            <Routes>
              <Route path="/app" element={<HomeView />} />
              <Route
                path="/video/:id"
                element={<VideoWorkspaceView key={pathname} />}
              />
              <Route
                path="/settings"
                element={<Navigate to="/account" replace />}
              />
              <Route path="/library" element={<LibraryView />} />
              <Route path="/account" element={<AccountView />} />
              <Route path="/subscription" element={<SubscriptionView />} />
              <Route
                path="*"
                element={
                  <div className="preferences-page">
                    <h1>页面未找到</h1>
                    <p>链接可能有误，请返回工作台重新开始。</p>
                    <Link className="button primary" to="/app">
                      返回工作台
                    </Link>
                  </div>
                }
              />
            </Routes>
            <PageReady />
          </Suspense>
        </PageErrorBoundary>
      </main>
      {pathname !== "/app" && <AppFooter />}
      <TaskHistory />
    </div>
  );
}
