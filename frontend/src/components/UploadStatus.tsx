import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useWorkspace } from "../hooks/useWorkspace";
import { ApiError, normalizeError } from "../api/client";
import { filesize } from "../utils";
import { isSending } from "../stores/uploads";
import type { UploadTransfer } from "../types/uploads";
import ErrorNotice from "./ErrorNotice";
import Icon from "./Icon";

export default function UploadStatus({
  entry,
  compact = false,
}: {
  entry: UploadTransfer;
  compact?: boolean;
}) {
  const store = useWorkspace(),
    input = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<ApiError | null>(null),
    [busy, setBusy] = useState(false);
  const noticeError = useMemo(
    () =>
      error ||
      new ApiError(
        entry.errorCode || "UPLOAD_INTERRUPTED",
        entry.error || "上传中断",
      ),
    [error, entry.errorCode, entry.error],
  );
  const sending = isSending(entry),
    cancelling = entry.phase === "cancelling";
  const percent = Math.min(
    100,
    Math.floor((entry.loaded / entry.record.expected_bytes) * 100),
  );
  const title = {
    sending: "正在上传文件",
    confirming: "正在确认上传",
    validating: "正在检查格式、音轨和时长",
    ready: "文件已就绪",
    interrupted:
      entry.record.status === "uploaded"
        ? "文件已上传，等待继续校验"
        : "上传尚未完成",
    cancelling: "正在取消并清理上传",
    cancelled: "原文件已移除或到期",
    failed: "文件校验未完成",
  }[entry.phase];
  async function retry(file?: File) {
    setBusy(true);
    setError(null);
    try {
      await store.uploads.resume(entry.id, file);
    } catch (cause) {
      setError(normalizeError(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className={`upload-status ${compact ? "upload-status-compact" : ""}`}
      aria-label="文件上传状态"
    >
      <div className="upload-status-heading">
        <span
          className={
            sending || entry.phase === "validating"
              ? "upload-status-mark active"
              : "upload-status-mark"
          }
        >
          <Icon name={entry.phase === "ready" ? "check" : "upload"} size={20} />
        </span>
        <div>
          <strong>{title}</strong>
          <p>
            {compact
              ? entry.record.filename
              : sending
                ? `${filesize(entry.loaded)} / ${filesize(entry.record.expected_bytes)}`
                : entry.phase === "validating"
                  ? "上传已完成，可以离开页面，稍后从任务历史继续。"
                  : entry.phase === "interrupted"
                    ? "会先核对服务器状态，已上传的文件将直接继续校验。"
                    : "上传与基础校验不消耗转写或 AI 额度。"}
          </p>
        </div>
        {sending && <span className="upload-percent">{percent}%</span>}
        {(sending || cancelling) && (
          <button
            className="text-button"
            disabled={cancelling || busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                await store.uploads.remove(entry.id, true);
              } catch (cause) {
                setError(normalizeError(cause));
              } finally {
                setBusy(false);
              }
            }}
          >
            {cancelling ? "正在取消…" : "取消上传"}
          </button>
        )}
      </div>
      {entry.phase === "sending" && (
        <div
          className="upload-progress"
          role="progressbar"
          aria-label="文件已上传"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
        >
          <span style={{ width: percent + "%" }} />
        </div>
      )}
      {entry.phase === "confirming" && (
        <p className="upload-stage-note" role="status">
          文件已发送，正在等待服务器确认。请暂时保留此页面。
        </p>
      )}
      {(entry.error || error) &&
        !["ready", "cancelled"].includes(entry.phase) && (
          <ErrorNotice error={noticeError} />
        )}
      {entry.phase === "interrupted" && (
        <div className="upload-recovery-actions">
          <button
            className="button secondary"
            disabled={busy}
            onClick={() =>
              entry.record.status === "uploaded" ||
              store.uploads.hasFile(entry.id)
                ? void retry()
                : input.current?.click()
            }
          >
            {busy
              ? "正在核对…"
              : entry.record.status === "uploaded"
                ? "继续校验"
                : store.uploads.hasFile(entry.id)
                  ? "重新上传"
                  : "重新选择原文件"}
          </button>
          <Link className="text-button" to="/app">
            导入其他文件
          </Link>
          <input
            ref={input}
            type="file"
            hidden
            accept=".mp4,.mov,.mkv,.webm,.mp3,.m4a"
            aria-label="重新选择原文件"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void retry(file);
              event.target.value = "";
            }}
          />
        </div>
      )}
      {entry.phase === "failed" && (
        <Link className="button secondary" to="/app">
          重新选择文件
        </Link>
      )}
    </section>
  );
}
export function UploadTray() {
  const store = useWorkspace(),
    { pathname } = useLocation();
  const entry = store.uploads.entries.find(
    (item) => isSending(item) || item.phase === "cancelling",
  );
  useEffect(() => {
    if (!entry) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [entry?.id, entry?.phase]);
  let selected = "";
  try {
    selected = decodeURIComponent(pathname.replace(/^\/video\//, ""));
  } catch {
    /* invalid routes have no selected content */
  }
  if (!entry || store.findContent(selected)?.source?.uploadId === entry.id)
    return null;
  return (
    <aside className="global-upload-tray" aria-label="正在上传">
      <UploadStatus entry={entry} compact />
      <Link to={`/video/${encodeURIComponent(entry.key)}`}>
        查看上传 <Icon name="arrow" size={14} />
      </Link>
    </aside>
  );
}
