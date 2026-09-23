import { Link } from "react-router-dom";

import { formatDuration } from "../api/client";
import { useVideoHistory } from "../hooks/useVideos";

export function HistoryPage() {
  const { data, isPending, isError } = useVideoHistory(1);

  return (
    <div className="mx-auto max-w-2xl pt-10">
      <p className="eyebrow">History</p>
      <h1 className="page-title mt-3">历史记录</h1>
      <p className="mt-1 text-sm text-muted">你解析过的视频都在这里，刷新不丢失。</p>

      <div className="mt-6 space-y-3">
        {isPending && <div className="card p-5 text-sm text-muted">加载中…</div>}
        {isError && (
          <div className="card p-5 text-sm text-muted">加载失败，请重新登录后再试。</div>
        )}
        {data?.items.length === 0 && (
          <div className="card p-5 text-sm text-muted">
            还没有解析记录，
            <Link to="/" className="underline underline-offset-4 hover:text-ink">
              去解析第一个视频
            </Link>
            。
          </div>
        )}
        {data?.items.map((v) => (
          <Link key={v.id} to={`/?video=${v.id}`} className="card block p-4 transition-colors hover:border-ink">
            <p className="truncate text-sm font-medium">{v.title}</p>
            <p className="mt-1 meta">
              {[v.platform, formatDuration(v.duration_seconds), v.status]
                .filter(Boolean)
                .join(" · ")}
            </p>
          </Link>
        ))}
      </div>
    </div>
  );
}
