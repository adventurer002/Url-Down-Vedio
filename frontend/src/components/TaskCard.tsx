import { useEffect, useRef, useState } from "react";
import type { LocalTask } from "../stores/workspace";
import { useWorkspace } from "../hooks/useWorkspace";
import { terminal } from "../lib/taskTracker";
import { ApiError, resourceAccessUrl, normalizeError } from "../api/client";
import { useAuth } from "../stores/auth";
import ErrorNotice from "./ErrorNotice";
import { clipError, isClipTask } from "../lib/clips";
import Icon from "./Icon";
import { Link } from "react-router-dom";
import { api } from "../api";
import { billingStore, useBilling } from "../stores/billing";
const labels = {
  queued: "等待执行",
  running: "执行中",
  processing: "处理中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  expired: "已过期",
};
export const stages: Record<string, string> = {
  preparing_source: "准备源视频",
  acquiring_source: "准备源视频",
  probing: "检查源视频",
  aligning: "核对片段起止位置",
  rendering: "生成视频片段",
  acquiring_transcript: "准备分析文稿",
  selecting: "寻找完整的精彩观点",
  ranking: "比较和筛选候选",
  committing: "核对并保存结果",
  waiting_for_clip_render_limit: "今日视频处理次数已达上限",
  waiting_for_storage: "资源暂忙，正在等待",
  media_probe: "检查格式、音轨和时长",
  waiting_for_allowance: "等待补充额度",
  waiting_for_entitlement: "等待有效权益",
  waiting_for_capacity: "等待执行空位",
  waiting_for_daily_downloads: "今日免费下载次数已用完",
  waiting_for_quote_required: "需要重新确认额度",
  queued: "等待开始处理",
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
  const { user } = useAuth();
  const billing = useBilling();
  const usage = billing.usage[task.id];
  const determinate =
    task.kind === "download" &&
    ["download", "downloading"].includes(task.event.stage);
  const completed = task.event.status === "completed";
  useEffect(() => {
    if (completed && user) void billingStore.loadUsage(task.id);
  }, [completed, task.id, user?.id]);
  const downloadRequest = useRef<AbortController | null>(null);
  const [downloading, setDownloading] = useState(false);
  useEffect(() => {
    setDownloading(false);
    return () => {
      downloadRequest.current?.abort();
      downloadRequest.current = null;
    };
  }, [user?.id, task.id]);
  const store = useWorkspace(),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<ApiError | null>(null);
  async function cancel() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await store.cancel(task);
    } catch (e) {
      setError(normalizeError(e));
    } finally {
      setBusy(false);
    }
  }
  async function download() {
    const fileId = task.snapshot?.result_payload?.file_id;
    if (!fileId || downloadRequest.current) return;
    const controller = new AbortController();
    downloadRequest.current = controller;
    setDownloading(true);
    setError(null);
    try {
      const url = await resourceAccessUrl(
        `/files/${encodeURIComponent(fileId)}/download`,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "";
      anchor.referrerPolicy = "no-referrer";
      anchor.click();
    } catch (cause) {
      if (!controller.signal.aborted) setError(normalizeError(cause));
    } finally {
      if (downloadRequest.current === controller) {
        downloadRequest.current = null;
        setDownloading(false);
      }
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
          {determinate && <b>{task.event.progress}%</b>}
        </span>
      </div>
      <div
        className={`progress-track${!determinate && !terminal(task.event.status) ? " indeterminate" : ""}`}
        role="progressbar"
        aria-valuenow={
          determinate || completed ? task.event.progress : undefined
        }
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={task.label}
      >
        <div
          style={{
            width: completed
              ? "100%"
              : determinate
                ? task.event.progress + "%"
                : "32%",
          }}
        />
      </div>
      <div className="task-bottom">
        <small>
          {stages[task.event.stage] || labels[task.event.status]}
          {task.snapshot?.result_payload?.cache_hit && <span> · 来自缓存</span>}
        </small>
        {[
          "waiting_for_allowance",
          "waiting_for_entitlement",
          "waiting_for_daily_downloads",
        ].includes(task.event.stage) && (
          <Link className="task-allowance-link" to="/account">
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
              setError(null);
              try {
                await api.resumeQueued();
                await billingStore.refresh();
              } catch (cause) {
                setError(normalizeError(cause));
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
            <button
              className="save-link"
              disabled={downloading}
              onClick={() => void download()}
            >
              <Icon name="download" size={14} />
              {downloading ? "准备下载…" : "保存到本地"}
            </button>
          )}
      </div>
      {completed && usage && (
        <p className="task-usage">
          实际使用：转写{" "}
          {usage.asr_seconds
            ? `${Math.floor(usage.asr_seconds / 60)} 分 ${usage.asr_seconds % 60} 秒`
            : "0"}{" "}
          · AI {usage.ai_generations} 次
        </p>
      )}
      {task.event.error_code && task.event.error_code !== "TASK_CANCELLED" && (
        <ErrorNotice
          placement="inline"
          error={
            isClipTask(task.kind)
              ? clipError(
                  new ApiError(
                    task.event.error_code,
                    task.snapshot?.error_message || "这项任务未能完成",
                  ),
                )
              : new ApiError(
                  task.event.error_code,
                  task.snapshot?.error_message || "这项任务未能完成",
                )
          }
        />
      )}
      <ErrorNotice error={error} />
      {task.trackingError && (
        <div className="task-error" role="status">
          <p>任务进度暂时未更新，正在尝试重新连接。</p>
          <details className="error-details">
            <summary>查看详情</summary>
            <p>{task.trackingError}</p>
          </details>
        </div>
      )}
    </div>
  );
}
