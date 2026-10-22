import { lazy, Suspense, useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import AppHeader from "./components/AppHeader";
import AppFooter from "./components/AppFooter";
import TaskHistory from "./components/TaskHistory";
import HomeView from "./views/HomeView";
import { useWorkspace } from "./hooks/useWorkspace";
const VideoWorkspaceView = lazy(() => import("./views/VideoWorkspaceView"));

export default function App() {
  const store = useWorkspace();
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
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </main>
      <AppFooter />
      <TaskHistory />
    </>
  );
}
