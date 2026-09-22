import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { Footer, Nav } from "./components/Nav";
import { LoginPage, RegisterPage } from "./pages/Auth";
import { HistoryPage } from "./pages/History";
import { HomePage } from "./pages/Home";
import { useAuthStore } from "./stores/auth";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});

function RequireAuth({ children }: { children: JSX.Element }) {
  const loggedIn = useAuthStore((s) => !!s.accessToken);
  if (!loggedIn) return <Navigate to="/login" replace />;
  return children;
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div className="flex min-h-screen flex-col">
          <Nav />
          <main className="mx-auto w-full max-w-5xl flex-1 px-6 pb-16">
            <Routes>
              <Route path="/" element={<HomePage />} />
              <Route path="/login" element={<LoginPage />} />
              <Route path="/register" element={<RegisterPage />} />
              <Route
                path="/history"
                element={
                  <RequireAuth>
                    <HistoryPage />
                  </RequireAuth>
                }
              />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </main>
          <Footer />
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
