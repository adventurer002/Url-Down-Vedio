import { useState } from "react";
import type { LocalTask } from "../stores/workspace";
import { useWorkspace } from "../hooks/useWorkspace";
import { terminal } from "../lib/taskTracker";
import { fileDownloadUrl, normalizeError } from "../api/client";
import Icon from "./Icon";
import { Link } from "react-router-dom";
import { api } from "../api";
import { billingStore } from "../stores/billing";
const labels = {
  queued: "等待执行",
  running: "执行中",
  processing: "处理中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  expired: "已过期",
};
const stages: Record<string, string> = {
  waiting_for_allowance: "等待补充额度",
  waiting_for_entitlement: "等待有效权益",
  waiting_for_capacity: "等待执行空位",
  waiting_for_daily_downloads: "今日免费下载次数已用完",
  waiting_for_quote_required: "需要重新确认额度",
  queued: "等待 Worker",
  download: "下载媒体",
  downloading: "下载媒体",
  processing: "处理媒体",
  subtitles: "获取平台字幕",
  audio: "准备纯音频",
  asr: "语音转写",
  summary: "提炼章节要点",
  question: "检索文稿并回答",
  mindmap: "构建思维导图",
  ai: "整理内容",
  running: "正在执行",
  completed: "处理完成",
};
export default function TaskCard({ task }: { task: LocalTask }) {
  const store = useWorkspace(),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function cancel() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await store.cancel(task);
    } catch (e) {
      setError(normalizeError(e).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="task-card">
      <div className="task-line">
        <span>{task.label}</span>
        <span className="task-status" data-status={task.event.status}>
          {task.event.stage.startsWith("waiting_for_")
            ? stages[task.event.stage] || "等待执行"
            : labels[task.event.status]}{" "}
          {task.event.status !== "queued" && <b>{task.event.progress}%</b>}
        </span>
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-valuenow={task.event.progress}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={task.label}
      >
        <div style={{ width: task.event.progress + "%" }} />
      </div>
      <div className="task-bottom">
        <small>
          {stages[task.event.stage] || labels[task.event.status]}
          {task.snapshot?.result_payload?.cache_hit && <span> · 来自缓存</span>}
        </small>
        {task.event.stage.startsWith("waiting_for_") && (
          <Link className="task-allowance-link" to="/subscription">
            查看额度
            <Icon name="arrow" size={12} />
          </Link>
        )}
        {["waiting_for_allowance", "waiting_for_entitlement"].includes(
          task.event.stage,
        ) && (
          <button
            className="text-button task-resume"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError("");
              try {
                await api.resumeQueued();
                await billingStore.refresh();
              } catch (cause) {
                setError(normalizeError(cause).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            重新检查
          </button>
        )}
        {!terminal(task.event.status) && (
          <button
            className="text-button"
            disabled={busy || task.snapshot?.cancel_requested}
            onClick={cancel}
          >
            {busy || task.snapshot?.cancel_requested ? "正在取消…" : "取消任务"}
          </button>
        )}
        {task.event.status === "completed" &&
          task.snapshot?.result_payload?.file_id && (
            <a
              className="save-link"
              href={fileDownloadUrl(task.snapshot.result_payload.file_id)}
            >
              <Icon name="download" size={14} />
              保存到本地
            </a>
          )}
      </div>
      {task.event.error_code && (
        <p className="task-error">
          {task.snapshot?.error_message || labels[task.event.status]} ·{" "}
          {task.event.error_code}
        </p>
      )}
      {(task.trackingError || error) && (
        <p className="task-error">
          {error || task.trackingError}
          {task.trackingError && <span> · 正在重连</span>}
        </p>
      )}
    </div>
  );
}
