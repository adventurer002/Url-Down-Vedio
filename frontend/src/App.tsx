import { authStore, useAuth } from "./stores/auth";
import { lazy, Suspense, useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import AppHeader from "./components/AppHeader";
import AppFooter from "./components/AppFooter";
import TaskHistory from "./components/TaskHistory";
import HomeView from "./views/HomeView";
import { useWorkspace } from "./hooks/useWorkspace";
import { billingStore } from "./stores/billing";
const LibraryView = lazy(() => import("./views/LibraryView"));
const SubscriptionView = lazy(() => import("./views/SubscriptionView"));
const SettingsView = lazy(() => import("./views/SettingsView"));
const AdminView = lazy(() => import("./views/AdminView"));
const InfoView = lazy(() => import("./views/InfoView"));
const VideoWorkspaceView = lazy(() => import("./views/VideoWorkspaceView"));

export default function App() {
  const store = useWorkspace();
  const auth = useAuth();
  useEffect(() => {
    void authStore.restore();
  }, []);
  useEffect(() => {
    store.switchUser(auth.user?.id || null);
    billingStore.setUser(auth.user?.id || null);
    if (auth.user?.status === "active") void billingStore.refresh();
    if (auth.user && authStore.hasAccess())
      void store.loadHistory().catch(() => {});
  }, [store, auth.user?.id, auth.user?.status, auth.user?.is_subscribed]);
  const { pathname } = useLocation();
  useEffect(() => {
    store.restore();
    return store.stopAll;
  }, [store]);
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [pathname]);
  return (
    <>
      <AppHeader />
      <main id="main">
        <Suspense
          fallback={
            <p className="muted" role="status">
              正在打开工作台…
            </p>
          }
        >
          <Routes>
            <Route path="/" element={<HomeView />} />
            <Route
              path="/video/:id"
              element={<VideoWorkspaceView key={pathname} />}
            />
            <Route path="/settings" element={<SettingsView />} />
            <Route path="/library" element={<LibraryView />} />
            <Route path="/subscription" element={<SubscriptionView />} />
            <Route path="/admin" element={<AdminView />} />
            <Route path="/help" element={<InfoView kind="help" />} />
            <Route path="/privacy" element={<InfoView kind="privacy" />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </main>
      {pathname !== "/" && <AppFooter />}
      <TaskHistory />
    </>
  );
}
