import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { normalizeError } from "../api/client";
import Icon from "../components/Icon";
import type { TaskStatus } from "./api";

export const statusLabels: Record<TaskStatus, string> = {
  queued: "排队中",
  running: "执行中",
  processing: "处理中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  expired: "已过期",
};
export const taskLabels: Record<string, string> = {
  download: "视频下载",
  audio_extraction: "音频提取",
  transcription: "文稿提取",
  ai_summary: "AI 总结",
  ai_mindmap: "思维导图",
  ai_question: "视频问答",
  simulate_progress: "进度模拟",
  model_test: "模型测试",
};
export function StatusBadge({ status }: { status: TaskStatus }) {
  return (
    <span className={`admin-badge admin-badge-${status}`}>
      {statusLabels[status] || status}
    </span>
  );
}
export function ErrorState({
  error,
  retry,
  retained = false,
}: {
  error: unknown;
  retry: () => void;
  retained?: boolean;
}) {
  const failure = normalizeError(error);
  return (
    <div className="admin-error" role="alert">
      <strong>{retained ? "更新失败，显示上次结果" : "数据暂不可用"}</strong>
      <p>{failure.message}</p>
      {failure.request_id && <small>请求编号：{failure.request_id}</small>}
      <button className="admin-button" onClick={retry}>
        重新加载
      </button>
    </div>
  );
}
export function Loading({
  label = "正在加载…",
  compact = false,
}: {
  label?: string;
  compact?: boolean;
}) {
  return (
    <div
      className={`admin-loading${compact ? " admin-loading-compact" : ""}`}
      role="status"
      aria-busy="true"
    >
      <div className="admin-loading-label">
        <span className="admin-spinner" aria-hidden="true" />
        {label}
      </div>
      {!compact && (
        <div className="admin-skeleton" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
      )}
    </div>
  );
}
export function PageSummary({
  items,
  busy = false,
}: {
  items: {
    label: string;
    value: number | undefined;
    note: string;
    icon: string;
  }[];
  busy?: boolean;
}) {
  return (
    <div
      className="admin-page-summary admin-page-summary-compact"
      aria-label="列表数据摘要"
      aria-busy={busy}
    >
      {items.map((item, index) => (
        <section
          key={item.label}
          className={
            index === 0 ? "admin-summary-total" : "admin-summary-page-count"
          }
        >
          <div>
            <p>{item.label}</p>
            <strong aria-label={`${item.label}：${item.value ?? "未知"}`}>
              {item.value == null && busy ? (
                <span className="admin-skeleton-value" aria-hidden="true" />
              ) : item.value == null ? (
                "—"
              ) : (
                item.value.toLocaleString("zh-CN")
              )}
            </strong>
            <small className={index === 0 ? undefined : "admin-sr-only"}>
              {item.note}
            </small>
          </div>
        </section>
      ))}
    </div>
  );
}

export function CopyId({
  value,
  label = "编号",
  children,
}: {
  value: string;
  label?: string;
  children?: ReactNode;
}) {
  const [feedback, setFeedback] = useState<"copied" | "error" | null>(null);
  const [copying, setCopying] = useState(false);
  useEffect(() => {
    setFeedback(null);
  }, [value]);
  useEffect(() => {
    if (feedback !== "copied") return;
    const timer = setTimeout(() => setFeedback(null), 2500);
    return () => clearTimeout(timer);
  }, [feedback]);
  return (
    <span className="admin-record-id">
      <span className="admin-id-value" title={value}>
        {children ?? <code>{shortId(value)}</code>}
      </span>
      <button
        type="button"
        className="admin-copy-id"
        aria-label={`复制${label} ${value}`}
        title={feedback === "copied" ? "已复制" : `复制完整${label}`}
        disabled={copying}
        onClick={async () => {
          setCopying(true);
          setFeedback(null);
          try {
            await navigator.clipboard.writeText(value);
            setFeedback("copied");
          } catch {
            setFeedback("error");
          } finally {
            setCopying(false);
          }
        }}
      >
        <Icon name={feedback === "copied" ? "check" : "copy"} size={14} />
      </button>
      <span role="status" className="admin-sr-only">
        {feedback === "copied" ? `${label}已复制` : ""}
      </span>
      {feedback === "error" && (
        <span className="admin-copy-error" role="status">
          复制失败，可手动复制：<code>{value}</code>
        </span>
      )}
    </span>
  );
}

export function DateTimeCell({ value }: { value?: string | null }) {
  const parsed = parseAdminDate(value);
  if (!parsed) return <span>—</span>;
  const [date, time] = formatDateTime(value).split(/\s+/);
  return (
    <time
      className="admin-date-cell"
      dateTime={parsed.toISOString()}
      aria-label={formatDateTime(value)}
    >
      <span>{date}</span>
      <span>{time}</span>
    </time>
  );
}
export function Panel({
  title,
  description,
  action,
  children,
  className = "",
  busy = false,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  busy?: boolean;
}) {
  return (
    <section className={`admin-panel ${className}`} aria-busy={busy}>
      {busy && <div className="admin-panel-progress" aria-hidden="true" />}
      <div className="admin-panel-heading">
        <div>
          <h2>{title}</h2>
          {description && <p>{description}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}
export function DetailDialog({
  title,
  description,
  trigger,
  children,
}: {
  title: string;
  description: string;
  trigger: ReactNode;
  children: ReactNode;
}) {
  return (
    <Dialog.Root>
      <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="admin-overlay" />
        <div className="admin-modal-viewport">
          <Dialog.Content className="admin-dialog">
            <Dialog.Title>{title}</Dialog.Title>
            <Dialog.Description>{description}</Dialog.Description>
            <Dialog.Close
              className="admin-icon-button admin-dialog-close"
              aria-label="关闭详情"
            >
              <Icon name="close" />
            </Dialog.Close>
            {children}
          </Dialog.Content>
        </div>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
function parseAdminDate(value?: string | null) {
  if (!value) return null;
  // PostgreSQL returns an offset; SQLite fixtures can return a naive UTC ISO date.
  const parsed = new Date(
    /T/.test(value) && !/(Z|[+-]\d{2}:\d{2})$/i.test(value)
      ? `${value}Z`
      : value,
  );
  return Number.isFinite(parsed.getTime()) ? parsed : null;
}
export function formatTime(value?: string | null, date = false) {
  const parsed = parseAdminDate(value);
  if (!parsed) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    ...(date ? ({ month: "2-digit", day: "2-digit" } as const) : {}),
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}
export function bytes(value: number | null | undefined) {
  if (value == null) return "未知";
  if (value === 0) return "0 B";
  const unit = Math.min(
    3,
    Math.floor(Math.log(Math.max(1, value)) / Math.log(1024)),
  );
  return `${(value / 1024 ** unit).toFixed(unit ? 1 : 0)} ${["B", "KiB", "MiB", "GiB"][unit]}`;
}
export function shortId(value?: string | null) {
  return value ? value.slice(0, 8) : "匿名任务";
}

export function formatDateTime(value?: string | null) {
  const parsed = parseAdminDate(value);
  return parsed
    ? new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(parsed)
    : "—";
}
