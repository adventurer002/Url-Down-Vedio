import { lazy, Suspense, useEffect } from "react";
import { useLocation } from "react-router-dom";
import { authStore, useAuth } from "./stores/auth";
import { useWorkspaceStore } from "./hooks/useWorkspace";
import { billingStore } from "./stores/billing";
import PageErrorBoundary from "./components/PageErrorBoundary";

import PublicApp from "./PublicApp";
import { pageLabel } from "./utils/navigation";
import { isMarketingPath } from "./marketing/routes";
const AdminView = lazy(() => import("./views/AdminView"));
const MarketingRoute = lazy(() => import("./marketing/ClientRoute"));

function WorkspaceApp() {
  const auth = useAuth();
  const store = useWorkspaceStore();
  const { pathname } = useLocation();
  useEffect(() => {
    void authStore.restore();
  }, []);
  useEffect(() => {
    if (auth.restoring || auth.restoreError) return;
    store.switchUser(auth.user?.id || null);
    billingStore.setUser(auth.user?.id || null);
  }, [store, auth.user?.id, auth.restoring, auth.restoreError]);
  const admin = pathname === "/admin" || pathname.startsWith("/admin/");
  useEffect(() => {
    if (!admin) return;
    let robots = document.querySelector<HTMLMetaElement>('meta[name="robots"]');
    if (!robots) {
      robots = document.createElement("meta");
      robots.name = "robots";
      document.head.appendChild(robots);
    }
    robots.content = "noindex,follow";
    document
      .querySelectorAll(
        'link[rel="canonical"], script[type="application/ld+json"], meta[property^="og:"], meta[name^="twitter:"]',
      )
      .forEach((item) => item.remove());
  }, [admin]);
  return (
    <PageErrorBoundary key={admin ? "admin" : "public"}>
      <Suspense
        fallback={
          <div role="status" className="app-loading" aria-busy="true">
            <span className="spinner" aria-hidden="true" />
            正在打开{pageLabel(pathname)}…
          </div>
        }
      >
        {admin ? <AdminView /> : <PublicApp />}
      </Suspense>
    </PageErrorBoundary>
  );
}

export default function App() {
  const { pathname } = useLocation();
  return isMarketingPath(pathname) ? (
    <Suspense fallback={<p role="status">正在打开页面…</p>}>
      <MarketingRoute path={pathname} />
    </Suspense>
  ) : (
    <WorkspaceApp />
  );
}
