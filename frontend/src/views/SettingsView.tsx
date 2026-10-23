import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { normalizeError, type ApiError } from "../api/client";
import type { CleanupResult } from "../types/api";
import ErrorNotice from "../components/ErrorNotice";
import { useAuth } from "../stores/auth";
function bytes(value: number) {
  return value < 1024
    ? `${value} B`
    : value < 1024 ** 2
      ? `${(value / 1024).toFixed(1)} KB`
      : `${(value / 1024 ** 2).toFixed(2)} MB`;
}
export default function SettingsView() {
  const { user } = useAuth();
  const [cleaning, setCleaning] = useState(false);
  const [result, setResult] = useState<CleanupResult>();
  const [error, setError] = useState<ApiError | null>(null);
  async function clean() {
    if (cleaning) return;
    setCleaning(true);
    setError(null);
    setResult(undefined);
    try {
      setResult(await api.cleanup());
    } catch (cause) {
      setError(normalizeError(cause));
    } finally {
      setCleaning(false);
    }
  }
  return (
    <div className="preferences-page">
      <Link className="back-link" to="/">
        ← 返回首页
      </Link>
      <h1>设置</h1>
      <p className="preferences-description">
        管理存储空间，让工作台保持轻盈。
      </p>
      <section className="preferences-card">
        <h2>存储空间</h2>
        <p>
          清理已过期的下载文件与可安全移除的临时文件。有效结果和运行中的任务会保留。
        </p>
        <button
          className="button secondary"
          disabled={cleaning}
          onClick={() => void clean()}
        >
          {cleaning ? "正在释放空间…" : "清理过期缓存文件"}
        </button>
        <ErrorNotice error={error} />
        {result && (
          <p className="cleanup-result" role="status">
            已释放 {bytes(result.freed_bytes)} ·{" "}
            {result.files + result.orphan_files} 个文件 /{" "}
            {result.temporary_directories} 个临时目录
          </p>
        )}
      </section>
      {user?.role === "admin" && (
        <Link className="back-link" to="/admin">
          查看服务运行状态 →
        </Link>
      )}
    </div>
  );
}
