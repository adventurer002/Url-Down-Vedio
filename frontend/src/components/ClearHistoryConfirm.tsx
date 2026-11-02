import { useEffect, useRef, useState } from "react";
import { normalizeError, type ApiError } from "../api/client";
import ErrorNotice from "./ErrorNotice";

export default function ClearHistoryConfirm({
  count,
  onConfirm,
  onClose,
}: {
  count: number;
  onConfirm: () => Promise<void>;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const lock = useRef(false);
  const alive = useRef(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    alive.current = true;
    const previous = document.activeElement as HTMLElement | null;
    const element = dialog.current;
    element?.showModal();
    cancel.current?.focus();
    return () => {
      alive.current = false;
      element?.close();
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  async function confirm() {
    if (lock.current) return;
    lock.current = true;
    setPending(true);
    setError(null);
    try {
      await onConfirm();
      if (alive.current) onClose();
    } catch (cause) {
      if (alive.current) setError(normalizeError(cause));
    } finally {
      lock.current = false;
      if (alive.current) setPending(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="billing-dialog history-clear-dialog"
      aria-labelledby="clear-history-title"
      aria-describedby="clear-history-description"
      onCancel={(event) => {
        event.preventDefault();
        if (!pending) onClose();
      }}
    >
      <h2 id="clear-history-title">隐藏全部历史记录？</h2>
      <p id="clear-history-description">
        当前账号共有 {count}{" "}
        条历史记录。隐藏后将不再出现在历史列表中，正在执行的任务、源文件和知识库收藏仍会保留。
      </p>
      <ErrorNotice error={error} />
      <div className="quote-actions">
        <button
          ref={cancel}
          className="button secondary"
          disabled={pending}
          onClick={onClose}
        >
          保留记录
        </button>
        <button
          className="button primary"
          disabled={pending || count === 0}
          onClick={() => void confirm()}
        >
          {pending ? "正在隐藏…" : "确认隐藏记录"}
        </button>
      </div>
    </dialog>
  );
}
