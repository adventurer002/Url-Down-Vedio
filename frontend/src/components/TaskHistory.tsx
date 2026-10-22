import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useWorkspace } from "../hooks/useWorkspace";
import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import type { CleanupResult } from "../types/api";
import Icon from "./Icon";
import TaskCard from "./TaskCard";
import ErrorNotice from "./ErrorNotice";
function bytes(value: number) {
  return value < 1024
    ? `${value} B`
    : value < 1024 ** 2
      ? `${(value / 1024).toFixed(1)} KB`
      : `${(value / 1024 ** 2).toFixed(2)} MB`;
}
export default function TaskHistory() {
  const store = useWorkspace(),
    dialog = useRef<HTMLDialogElement>(null);
  const [error, setError] = useState<ApiError | null>(null),
    [cleaning, setCleaning] = useState(false),
    [cleanup, setCleanup] = useState<CleanupResult>();
  const refresh = useCallback(
    async (reset = true) => {
      setError(null);
      try {
        await store.loadHistory(reset);
      } catch (e) {
        setError(normalizeError(e));
      }
    },
    [store],
  );
  const close = () => store.setHistoryOpen(false);
  useEffect(() => {
    if (!store.historyOpen) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    const element = dialog.current;
    let disposed = false,
      timer: ReturnType<typeof setTimeout>;
    element?.showModal();
    async function sync(force = false) {
      if (force || !store.historyLoaded || store.historyOffset <= 20)
        await refresh();
      if (!disposed) timer = setTimeout(() => void sync(), 15000);
    }
    void sync(true);
    return () => {
      disposed = true;
      clearTimeout(timer);
      element?.close();
      previousFocus?.focus();
    };
  }, [store, store.historyOpen, refresh]);
  async function clean() {
    if (cleaning) return;
    setCleaning(true);
    setError(null);
    setCleanup(undefined);
    try {
      setCleanup(await api.cleanup());
      await refresh();
    } catch (e) {
      setError(normalizeError(e));
    } finally {
      setCleaning(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="history-dialog"
      aria-labelledby="history-title"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div className="drawer">
        <div className="drawer-heading">
          <div>
            <span className="eyebrow">YOUR ACTIVITY</span>
            <h2 id="history-title">任务历史</h2>
          </div>
          <button
            className="icon-button"
            aria-label="关闭任务历史"
            onClick={close}
          >
            <Icon name="close" />
          </button>
        </div>
        <div className="history-summary">
          <p className="muted history-hint">
            来自本地服务器 · {store.historyTotal} 个任务
          </p>
          <button
            className="text-button"
            disabled={store.historyLoading}
            onClick={() => void refresh()}
          >
            刷新列表
          </button>
        </div>
        <ErrorNotice error={error} />
        {store.historyLoading && !store.historyLoaded && (
          <p className="muted" role="status">
            正在读取服务器任务…
          </p>
        )}
        {!store.historyTasks.length && !store.historyLoading && (
          <div className="empty-state">
            <Icon name="history" size={36} />
            <h3>一切从一个链接开始</h3>
            <p>下载、转写与 AI 任务会记录在这里。</p>
          </div>
        )}
        {store.historyTasks.map((task) => (
          <article key={task.id} className="history-item">
            {task.videoAvailable !== false ? (
              <Link
                to={"/video/" + encodeURIComponent(task.videoKey)}
                onClick={close}
              >
                {task.title}
              </Link>
            ) : (
              <strong>{task.title}</strong>
            )}
            <time className="history-date">
              {new Date(task.createdAt).toLocaleString("zh-CN")}
            </time>
            <TaskCard task={task} />
            {task.videoAvailable !== false &&
              task.event.status === "completed" &&
              (task.snapshot?.result_payload?.result_id ||
                task.snapshot?.result_payload?.transcript_id) && (
                <Link
                  className="history-result-link"
                  to={"/video/" + encodeURIComponent(task.videoKey)}
                  onClick={close}
                >
                  查看文稿 / AI 结果 ↗
                </Link>
              )}
          </article>
        ))}
        {store.historyOffset < store.historyTotal && (
          <button
            className="button secondary full"
            disabled={store.historyLoading}
            onClick={() => void refresh(false)}
          >
            {store.historyLoading ? "正在读取…" : "加载更多任务"}
          </button>
        )}
        <div className="cleanup-section">
          <span className="eyebrow">KEEP YOUR SPACE CLEAR</span>
          <p>
            仅清理已过期文件与安全可移除的临时文件，保留当前有效结果和运行中的任务。
          </p>
          <button
            className="button secondary full"
            disabled={cleaning}
            onClick={clean}
          >
            {cleaning ? "正在释放空间…" : "清理过期缓存文件"}
          </button>
          {cleanup && (
            <p className="cleanup-result" role="status">
              已释放 {bytes(cleanup.freed_bytes)} ·{" "}
              {cleanup.files + cleanup.orphan_files} 个文件 /{" "}
              {cleanup.temporary_directories} 个临时目录
            </p>
          )}
        </div>
      </div>
    </dialog>
  );
}
