import { useEffect, useRef, useState } from "react";
import type { TranscriptResult } from "../types/api";
import type { Citation } from "../types/knowledge";
import TranscriptReader from "./TranscriptReader";
import Icon from "./Icon";

export default function CitationDrawer({
  transcript,
  focus,
  sourceUrl,
  onClose,
}: {
  transcript: TranscriptResult;
  focus: Citation;
  sourceUrl: string;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null),
    close = useRef<HTMLButtonElement>(null);
  const [narrow, setNarrow] = useState(
    () => window.matchMedia?.("(max-width: 1099px)").matches ?? true,
  );
  useEffect(() => {
    const media = window.matchMedia?.("(max-width: 1099px)");
    if (!media) return;
    const changed = () => setNarrow(media.matches);
    media.addEventListener?.("change", changed);
    return () => media.removeEventListener?.("change", changed);
  }, []);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    if (narrow) dialog.current?.showModal();
    else close.current?.focus();
    const escape = (event: KeyboardEvent) => {
      if (!narrow && event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", escape);
    return () => {
      dialog.current?.close();
      window.removeEventListener("keydown", escape);
      if (previous?.isConnected) previous.focus();
    };
  }, [narrow]);
  const content = (
    <>
      <div className="citation-drawer-heading">
        <div>
          <span>回到内容本身</span>
          <h2 id="citation-drawer-title">原文依据</h2>
        </div>
        <button
          ref={close}
          className="icon-button"
          aria-label="关闭原文依据"
          onClick={onClose}
        >
          <Icon name="close" />
        </button>
      </div>
      <TranscriptReader
        transcript={transcript}
        sourceUrl={sourceUrl}
        focus={focus}
        idPrefix="citation"
      />
    </>
  );
  return narrow ? (
    <dialog
      ref={dialog}
      className="citation-drawer citation-mobile"
      aria-labelledby="citation-drawer-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      {content}
    </dialog>
  ) : (
    <aside
      className="citation-drawer citation-desktop"
      aria-labelledby="citation-drawer-title"
    >
      {content}
    </aside>
  );
}
