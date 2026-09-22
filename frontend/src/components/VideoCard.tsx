import { useState } from "react";

import { formatBytes, formatDuration } from "../api/client";
import type { TaskDetail, VideoDetail } from "../types";

export function ErrorNote({ code, message }: { code?: string; message?: string }) {
  if (!message) return null;
  return (
    <div className="card border-ink bg-faint p-4 text-sm">
      <p className="font-medium">{message}</p>
      {code && <p className="mt-1 font-mono text-xs text-muted">{code}</p>}
    </div>
  );
}

export function ProgressBar({ value }: { value: number }) {
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-full bg-line"
      role="progressbar"
      aria-valuenow={Math.round(value)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-full rounded-full bg-ink transition-all duration-300"
        style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
      />
    </div>
  );
}

const STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  downloading: "下载中",
  processing: "处理中",
  uploading: "上传中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export function TaskStatusLine({ task }: { task: TaskDetail }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-sm">
        <span className="font-medium">{STATUS_LABEL[task.status] ?? task.status}</span>
        <span className="font-mono text-xs text-muted">{task.progress.toFixed(1)}%</span>
      </div>
      <ProgressBar value={task.progress} />
      {task.error_message && (
        <p className="text-sm text-muted">
          {task.error_message}
          {task.error_code && (
            <span className="ml-2 font-mono text-xs">({task.error_code})</span>
          )}
        </p>
      )}
    </div>
  );
}

export function VideoCard({
  video,
  selectedFormat,
  onSelectFormat,
}: {
  video: VideoDetail;
  selectedFormat: string;
  onSelectFormat: (formatId: string) => void;
}) {
  return (
    <div className="card overflow-hidden">
      <div className="flex flex-col gap-4 p-5 sm:flex-row">
        {video.thumbnail_url && (
          <img
            src={video.thumbnail_url}
            alt={video.title}
            className="h-32 w-full rounded-md border border-line object-cover sm:w-56"
          />
        )}
        <div className="min-w-0 flex-1">
          <p className="truncate text-base font-semibold">{video.title}</p>
          <p className="mt-1 font-mono text-xs text-muted">
            {[video.platform, video.uploader, formatDuration(video.duration_seconds)]
              .filter(Boolean)
              .join(" · ")}
          </p>
          {video.formats.length > 0 && (
            <div className="mt-3">
              <p className="label mb-2">清晰度</p>
              <div className="flex flex-wrap gap-2">
                {video.formats.map((f) => {
                  const active = f.format_id === selectedFormat;
                  return (
                    <button
                      key={f.format_id}
                      onClick={() => onSelectFormat(f.format_id)}
                      className={`rounded-md border px-3 py-1.5 font-mono text-xs transition-colors ${
                        active
                          ? "border-ink bg-ink text-paper"
                          : "border-line bg-paper text-ink hover:border-ink"
                      }`}
                    >
                      {f.resolution || f.format_id} · {f.ext}
                      {f.filesize ? ` · ${formatBytes(f.filesize)}` : ""}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export function UrlForm({
  onSubmit,
  loading,
  initial,
}: {
  onSubmit: (url: string) => void;
  loading: boolean;
  initial?: string;
}) {
  const [url, setUrl] = useState(initial ?? "");
  return (
    <form
      className="flex flex-col gap-2 sm:flex-row"
      onSubmit={(e) => {
        e.preventDefault();
        if (url.trim()) onSubmit(url.trim());
      }}
    >
      <input
        className="field !h-12 flex-1 !text-base"
        placeholder="粘贴视频链接，例如 https://…"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        inputMode="url"
        aria-label="视频链接"
      />
      <button type="submit" className="btn btn-primary !h-12 !px-6" disabled={loading || !url.trim()}>
        {loading ? "解析中…" : "解析"}
      </button>
    </form>
  );
}
