import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useWorkspace } from "../hooks/useWorkspace";
import { authStore, useAuth } from "../stores/auth";
import { ApiError, normalizeError } from "../api/client";
import Icon from "./Icon";
import TaskCard from "./TaskCard";
import ErrorNotice from "./ErrorNotice";
export default function TaskHistory() {
  const auth = useAuth();
  const store = useWorkspace(),
    dialog = useRef<HTMLDialogElement>(null);
  const [error, setError] = useState<ApiError | null>(null);
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
  async function remove(taskId?: string) {
    setError(null);
    try {
      await store.deleteHistory(taskId);
    } catch (e) {
      setError(normalizeError(e));
    }
  }
  const close = () => store.setHistoryOpen(false);
  useEffect(() => {
    if (!store.historyOpen) return;
    if (!authStore.requireLogin()) {
      store.setHistoryOpen(false);
      return;
    }
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
  }, [store, store.historyOpen, refresh, auth.user]);
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
          <p className="muted history-hint">共 {store.historyTotal} 个任务</p>
          <div className="history-toolbar">
            <button
              className="history-delete"
              disabled={
                store.historyLoading ||
                store.historyMutating ||
                !store.historyTotal
              }
              onClick={() => void remove()}
            >
              <Icon name="trash" size={15} />
              {store.historyMutating ? "正在删除…" : "一键清除"}
            </button>
            <button
              className="text-button"
              disabled={store.historyLoading || store.historyMutating}
              onClick={() => void refresh()}
            >
              刷新列表
            </button>
          </div>
        </div>
        <p className="history-delete-hint">
          删除仅移除历史记录，不取消任务或删除文件。
        </p>
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
            <div className="history-item-heading">
              {task.videoAvailable !== false ? (
                <Link
                  to={
                    "/video/" +
                    encodeURIComponent(task.videoKey) +
                    (task.kind === "ai_question"
                      ? "?" +
                        new URLSearchParams({
                          tab: "questions",
                          transcript:
                            task.snapshot?.result_payload?.transcript_id || "",
                          answer:
                            task.snapshot?.result_payload?.answer_id || "",
                        })
                      : "")
                  }
                  onClick={close}
                >
                  {task.title}
                </Link>
              ) : (
                <strong>{task.title}</strong>
              )}
              <button
                className="history-delete"
                aria-label={`删除任务记录：${task.title}`}
                disabled={store.historyLoading || store.historyMutating}
                onClick={() => void remove(task.id)}
              >
                <Icon name="trash" size={15} />
                删除
              </button>
            </div>
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
                  to={
                    "/video/" +
                    encodeURIComponent(task.videoKey) +
                    (task.kind === "ai_question"
                      ? "?" +
                        new URLSearchParams({
                          tab: "questions",
                          transcript:
                            task.snapshot?.result_payload?.transcript_id || "",
                          answer:
                            task.snapshot?.result_payload?.answer_id || "",
                        })
                      : "")
                  }
                  onClick={close}
                >
                  {task.kind === "ai_question"
                    ? "查看问答与原文 ↗"
                    : "查看文稿 / AI 结果 ↗"}
                </Link>
              )}
          </article>
        ))}
        {store.historyOffset < store.historyTotal && (
          <button
            className="button secondary full"
            disabled={store.historyLoading || store.historyMutating}
            onClick={() => void refresh(false)}
          >
            {store.historyLoading ? "正在读取…" : "加载更多任务"}
          </button>
        )}
      </div>
    </dialog>
  );
}
