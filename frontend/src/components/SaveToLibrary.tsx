import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { normalizeError, type ApiError } from "../api/client";
import { useAuth, authStore } from "../stores/auth";
import type { LibraryItem } from "../types/knowledge";
import ErrorNotice from "./ErrorNotice";
import Icon from "./Icon";

export default function SaveToLibrary({
  transcriptId,
  resultId,
  onSaved,
}: {
  transcriptId: string;
  resultId?: string;
  onSaved?: () => void;
}) {
  const { user } = useAuth();
  return user ? (
    <SaveControl
      key={user.id + transcriptId}
      transcriptId={transcriptId}
      resultId={resultId}
      onSaved={onSaved}
      userId={user.id}
    />
  ) : null;
}
function SaveControl({
  transcriptId,
  resultId,
  userId,
  onSaved,
}: {
  transcriptId: string;
  resultId?: string;
  userId: string;
  onSaved?: () => void;
}) {
  const [saved, setSaved] = useState<LibraryItem>(),
    [open, setOpen] = useState(false),
    [tags, setTags] = useState(""),
    [error, setError] = useState<ApiError | null>(null),
    [pending, setPending] = useState(false),
    [loaded, setLoaded] = useState(false),
    [attempt, setAttempt] = useState(0);
  const alive = useRef(true),
    lock = useRef(false),
    dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    setLoaded(false);
    setError(null);
    void api
      .library("", "", 0, controller.signal, transcriptId)
      .then((data) => {
        if (!controller.signal.aborted) {
          setSaved(data.items[0]);
          setTags(data.items[0]?.tags.join("，") || "");
          setLoaded(true);
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(normalizeError(cause));
      });
    return () => {
      alive.current = false;
      controller.abort();
    };
  }, [transcriptId, attempt]);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    return () => {
      dialog.current?.close();
      previous?.focus();
    };
  }, [open]);
  async function save() {
    if (lock.current) return;
    lock.current = true;
    setPending(true);
    setError(null);
    try {
      const value = await api.saveVideo(
        transcriptId,
        resultId,
        tags
          .split(/[,，\n]/)
          .map((value) => value.trim())
          .filter(Boolean),
      );
      if (alive.current && authStore.getState().user?.id === userId) {
        setSaved(value);
        setTags(value.tags.join("，"));
        setOpen(false);
        onSaved?.();
      }
    } catch (cause) {
      if (alive.current) setError(normalizeError(cause));
    } finally {
      lock.current = false;
      if (alive.current) setPending(false);
    }
  }
  return (
    <div className="library-save-control">
      <button
        className="button secondary"
        disabled={!loaded}
        onClick={() => setOpen(true)}
      >
        <Icon name="bookmark" size={16} />
        {saved ? "已收藏 · 编辑标签" : "收藏到知识库"}
      </button>
      <Link to="/library">查看知识库 →</Link>
      {!open && (
        <ErrorNotice error={error}>
          <button
            className="text-button"
            onClick={() => setAttempt((value) => value + 1)}
          >
            重试读取收藏
          </button>
        </ErrorNotice>
      )}
      {open && (
        <dialog
          ref={dialog}
          className="billing-dialog"
          aria-labelledby="save-title"
          onCancel={(event) => {
            event.preventDefault();
            if (!pending) setOpen(false);
          }}
        >
          <button
            className="dialog-close"
            aria-label="关闭收藏"
            disabled={pending}
            onClick={() => setOpen(false)}
          >
            <Icon name="close" />
          </button>
          <span className="eyebrow">KEEP WHAT MATTERS</span>
          <h2 id="save-title">留在你的知识库</h2>
          <p>
            保存当前文稿和已生成的总结，之后可按标题、内容和标签搜索。收藏与查看免费。
          </p>
          <label htmlFor="save-tags">标签</label>
          <input
            id="save-tags"
            maxLength={350}
            value={tags}
            onChange={(event) => setTags(event.target.value)}
            placeholder="例如：产品设计，学习笔记"
          />
          <small className="muted">
            逗号分隔，最多 12 个，每个不超过 24 字
          </small>
          <ErrorNotice error={error} />
          <button
            className="button primary full"
            disabled={pending}
            onClick={() => void save()}
          >
            {pending ? "正在保存…" : "保存收藏"}
          </button>
        </dialog>
      )}
    </div>
  );
}
