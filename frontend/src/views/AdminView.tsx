import { Link } from "react-router-dom";
import { useAuth } from "../stores/auth";
import { useEffect, useState } from "react";
import { api } from "../api";
import { get } from "../api/client";
import type { LivenessStatus } from "../types/api";
function ServiceHealth() {
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
    <section className="preferences-card">
      <h2>服务状态</h2>
      <p>每 15 秒更新一次。</p>
      <div className="health-states" aria-live="polite">
        {Object.entries(states).map(([name, state]) => (
          <div key={name} className="health">
            <span>{name}</span>
            <strong>
              {state === "ready"
                ? "已就绪"
                : state === "checking"
                  ? "检查中"
                  : state === "unknown"
                    ? "状态未知"
                    : "无法连接"}
            </strong>
          </div>
        ))}
      </div>
    </section>
  );
}
export default function AdminView() {
  const { user } = useAuth();
  return (
    <div className="preferences-page">
      <Link className="back-link" to="/">
        ← 返回首页
      </Link>
      <h1>管理界面</h1>
      {user?.role === "admin" ? (
        <ServiceHealth />
      ) : (
        <p className="preferences-description">
          此页面仅供管理员查看服务运行状态。
        </p>
      )}
    </div>
  );
}
