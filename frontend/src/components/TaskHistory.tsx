import { taskLocation, sourceLabel } from "../lib/content";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useWorkspace } from "../hooks/useWorkspace";
import { authStore, useAuth } from "../stores/auth";
import { ApiError, normalizeError } from "../api/client";
import Icon from "./Icon";
import TaskCard from "./TaskCard";
import ErrorNotice from "./ErrorNotice";
import ClearHistoryConfirm from "./ClearHistoryConfirm";
export default function TaskHistory() {
  const auth = useAuth();
  const store = useWorkspace(),
    dialog = useRef<HTMLDialogElement>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [clearUserId, setClearUserId] = useState<string | null>(null);
  useEffect(() => setClearUserId(null), [auth.user?.id, store.historyOpen]);
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
    <>
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
                onClick={() => setClearUserId(auth.user?.id || null)}
              >
                <Icon name="trash" size={15} />
                {store.historyMutating ? "正在删除…" : "删除全部记录"}
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
          <p className="history-delete-hint">删除的历史记录无法恢复。</p>
          <ErrorNotice error={error} />
          {store.historyLoading && !store.historyLoaded && (
            <p className="muted" role="status">
              正在读取服务器任务…
            </p>
          )}
          {!store.historyTasks.length && !store.historyLoading && (
            <div className="empty-state">
              <Icon name="history" size={36} />
              <h3>从第一份内容开始</h3>
              <p>链接、本地视频和录音的处理记录都会保存在这里。</p>
            </div>
          )}
          {store.historyTasks.map((task) => (
            <article key={task.id} className="history-item">
              <div className="history-item-heading">
                {task.videoAvailable !== false ||
                task.kind === "ai_clip_plan" ||
                task.kind === "clips_export" ? (
                  <Link to={taskLocation(task)} onClick={close}>
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
              {store.findContent(task.videoKey) && (
                <span className="history-source-label">
                  {sourceLabel(store.findContent(task.videoKey)!)}
                </span>
              )}
              <time className="history-date">
                {new Date(task.createdAt).toLocaleString("zh-CN")}
              </time>
              <TaskCard task={task} />
              {(task.videoAvailable !== false ||
                task.kind === "ai_clip_plan" ||
                task.kind === "clips_export") &&
                task.event.status === "completed" &&
                (task.snapshot?.result_payload?.result_id ||
                  task.snapshot?.result_payload?.clip_plan_id ||
                  task.snapshot?.result_payload?.clip_export_id ||
                  task.snapshot?.result_payload?.transcript_id) && (
                  <Link
                    className="history-result-link"
                    to={taskLocation(task)}
                    onClick={close}
                  >
                    {task.kind === "ai_clip_plan" ||
                    task.kind === "clips_export"
                      ? "查看切片候选 ↗"
                      : task.kind === "ai_question"
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
      {store.historyOpen && clearUserId && clearUserId === auth.user?.id && (
        <ClearHistoryConfirm
          key={clearUserId}
          count={store.historyTotal}
          onConfirm={async () => {
            if (authStore.getState().user?.id !== clearUserId) return;
            await store.deleteHistory();
          }}
          onClose={() => setClearUserId(null)}
        />
      )}
    </>
  );
}
