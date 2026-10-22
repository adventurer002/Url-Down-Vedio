import { useEffect, useState } from "react";
import { api } from "../api";
import { get } from "../api/client";
import type { LivenessStatus } from "../types/api";
export default function AppFooter() {
  const [states, setStates] = useState({
    API: "checking",
    PostgreSQL: "checking",
    Redis: "checking",
  });
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController;
    async function refresh() {
      controller = new AbortController();
      try {
        await api.ready(controller.signal);
        if (!disposed)
          setStates({ API: "ready", PostgreSQL: "ready", Redis: "ready" });
      } catch {
        if (!disposed) {
          setStates({
            API: "unavailable",
            PostgreSQL: "unknown",
            Redis: "unknown",
          });
          try {
            await get<LivenessStatus>("/health/live", controller.signal);
            if (!disposed)
              setStates((previous) => ({ ...previous, API: "ready" }));
          } catch {
            /* Preserve unavailable state. */
          }
        }
      }
      if (!disposed) timer = setTimeout(refresh, 15000);
    }
    void refresh();
    return () => {
      disposed = true;
      clearTimeout(timer);
      controller?.abort();
    };
  }, []);
  return (
    <footer className="app-footer">
      <span>
        VIDEOHUB AI <span className="footer-divider">/</span>{" "}
        让内容成为你的知识。
      </span>
      <div className="health-states" aria-live="polite">
        {Object.entries(states).map(([name, state]) => (
          <span
            key={name}
            title={
              state === "ready"
                ? "已就绪"
                : state === "checking"
                  ? "检查中"
                  : state === "unknown"
                    ? "依赖状态未知"
                    : "无法连接"
            }
            className="health"
          >
            <i
              className={
                state === "ready"
                  ? "ready"
                  : state === "unavailable"
                    ? "down"
                    : ""
              }
            />
            {name}
            <span className="sr-only">{state}</span>
          </span>
        ))}
      </div>
    </footer>
  );
}
