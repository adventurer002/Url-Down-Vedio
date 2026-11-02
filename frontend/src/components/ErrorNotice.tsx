import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { ApiError } from "../api/client";
import { acquireNoticeLayer } from "../lib/noticeLayer";
import { errorPresentation } from "../utils/errorPresentation";
import Icon from "./Icon";
import "../notices.css";

const occurrences = new WeakMap<ApiError, number>();
let nextOccurrence = 0;

interface Props {
  error: ApiError | null;
  children?: ReactNode;
  placement?: "floating" | "inline";
  id?: string;
  onDismiss?: () => void;
}
export default function ErrorNotice(props: Props) {
  if (!props.error) return null;
  const { code, request_id, message } = props.error;
  let occurrence = occurrences.get(props.error);
  if (!occurrence) {
    occurrence = ++nextOccurrence;
    occurrences.set(props.error, occurrence);
  }
  const key =
    props.placement === "inline"
      ? JSON.stringify([code, request_id, message])
      : occurrence;
  return <DismissibleNotice key={key} {...props} error={props.error} />;
}
function DismissibleNotice({
  error,
  children,
  placement = "floating",
  id,
  onDismiss,
}: Props & { error: ApiError }) {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;
  const presentation = errorPresentation(error);
  const content = (
    <div
      className={
        placement === "floating"
          ? "floating-notice"
          : "error-notice inline-notice"
      }
      role="alert"
      id={id}
    >
      {placement === "floating" && (
        <span className="notice-symbol">
          <Icon name="warning" size={20} />
        </span>
      )}
      <div className="notice-content">
        <strong>{presentation.message}</strong>
        {presentation.suggestion && (
          <p className="error-guidance">{presentation.suggestion}</p>
        )}
        {children && <div className="error-actions">{children}</div>}
        <details className="error-details">
          <summary>查看详情</summary>
          <div>
            {presentation.message !== error.message && <p>{error.message}</p>}
            <p>
              错误代码：<code>{error.code}</code>
            </p>
            {error.request_id && <p>请求编号：{error.request_id}</p>}
          </div>
        </details>
      </div>
      <button
        type="button"
        className="notice-close"
        aria-label="关闭错误提示"
        onClick={() => {
          setDismissed(true);
          onDismiss?.();
        }}
      >
        <Icon name="close" size={18} />
      </button>
    </div>
  );
  return placement === "inline" ? (
    content
  ) : (
    <FloatingPortal>{content}</FloatingPortal>
  );
}
function FloatingPortal({ children }: { children: ReactNode }) {
  const anchor = useRef<HTMLSpanElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const [container, setContainer] = useState<HTMLElement>();
  useLayoutEffect(() => {
    // A popup raised from a modal must remain in its subtree to stay interactive.
    const owner = anchor.current?.closest("dialog") || document.body;
    const previous = document.activeElement as HTMLElement | null;
    const layer = acquireNoticeLayer(owner);
    setContainer(layer.element);
    return () => {
      const restoreFocus = content.current?.contains(document.activeElement);
      layer.release();
      if (restoreFocus && previous?.isConnected)
        previous.focus({ preventScroll: true });
    };
  }, []);
  return (
    <>
      <span hidden ref={anchor} aria-hidden="true" />
      {container &&
        createPortal(
          <div className="notice-item" ref={content}>
            {children}
          </div>,
          container,
        )}
    </>
  );
}
