import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { timecode } from "../../utils";
import type { ClipCandidate, ClipPlan } from "../../types/clips";
import Icon from "../Icon";

function CandidateDetail({
  candidate,
  transcriptUrl,
}: {
  candidate: ClipCandidate;
  transcriptUrl: string;
}) {
  const [notice, setNotice] = useState("");
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    setNotice("");
    return () => {
      alive.current = false;
    };
  }, [candidate.id]);
  return (
    <>
      <h3>{candidate.title}</h3>
      <p className="clip-time">
        约 {timecode(candidate.start)}–{timecode(candidate.end)} · 待定位
      </p>
      <blockquote>{candidate.quote}</blockquote>
      <div className="clip-detail-actions">
        <button
          className="text-button"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(candidate.quote);
              if (alive.current) setNotice("原文已复制");
            } catch {
              if (alive.current) setNotice("暂时无法复制，请选择原文后复制。");
            }
          }}
        >
          <Icon name="copy" size={15} />
          复制原文
        </button>
        <Link className="text-button" to={transcriptUrl}>
          查看对应文稿
        </Link>
      </div>
      <p className="clip-copy-status" role="status">
        {notice}
      </p>
      <div className="clip-reason">
        <h4>为什么入选</h4>
        <p>{candidate.reason}</p>
      </div>
    </>
  );
}
export default function ClipCandidateList({
  plan,
  contentKey,
  active: panelActive,
  selection,
}: {
  plan: ClipPlan;
  contentKey: string;
  active: boolean;
  selection?: {
    ids: string[];
    disabled: boolean;
    onChange: (ids: string[]) => void;
  };
}) {
  const [activeId, setActiveId] = useState(plan.candidates[0]?.id);
  const [opened, setOpened] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null),
    detail = useRef<HTMLElement>(null),
    titleId = useId();
  const active =
    plan.candidates.find((item) => item.id === activeId) || plan.candidates[0];
  const transcriptUrl = `/video/${encodeURIComponent(contentKey)}?${new URLSearchParams({ tab: "transcript", transcript: plan.transcript_id, clip_plan: plan.id })}`;
  useEffect(() => {
    if (!panelActive) setOpened(false);
  }, [panelActive]);
  useEffect(() => {
    if (!opened) return;
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    return () => {
      dialog.current?.close();
      if (previous?.isConnected && !previous.closest("[hidden]"))
        previous.focus();
    };
  }, [opened]);
  if (!active) return null;
  return (
    <div className="clip-candidates-layout">
      <div className="clip-candidates" role="list" aria-label="精彩候选">
        {plan.candidates.map((candidate) => (
          <article
            className={`clip-candidate${candidate.id === active.id ? " current" : ""}${selection?.ids.includes(candidate.id) ? " selected" : ""}`}
            key={candidate.id}
            role="listitem"
          >
            <div className="clip-candidate-title">
              {selection && (
                <label className="clip-select">
                  <input
                    type="checkbox"
                    aria-label={`选择片段：${candidate.title}`}
                    checked={selection.ids.includes(candidate.id)}
                    disabled={selection.disabled}
                    onChange={(event) =>
                      selection.onChange(
                        event.target.checked
                          ? [...selection.ids, candidate.id]
                          : selection.ids.filter((id) => id !== candidate.id),
                      )
                    }
                  />
                </label>
              )}
              <h3>{candidate.title}</h3>
            </div>
            <p className="clip-time">
              约 {timecode(candidate.start)}–{timecode(candidate.end)}
              <span>待定位</span>
            </p>
            <p className="clip-excerpt">{candidate.quote}</p>
            <div className="clip-candidate-actions">
              <span>{candidate.reason}</span>
              <button
                className="text-button"
                aria-label={`查看原文：${candidate.title}`}
                onClick={() => {
                  setActiveId(candidate.id);
                  if (window.matchMedia("(max-width: 1023px)").matches)
                    setOpened(true);
                  else setTimeout(() => detail.current?.focus(), 0);
                }}
              >
                查看原文
                <Icon name="arrow" size={14} />
              </button>
            </div>
          </article>
        ))}
      </div>
      <aside
        ref={detail}
        className="clip-detail"
        aria-label="片段原文"
        tabIndex={0}
      >
        <p className="clip-detail-label">片段原文</p>
        <CandidateDetail
          key={active.id}
          candidate={active}
          transcriptUrl={transcriptUrl}
        />
      </aside>
      {opened && (
        <dialog
          ref={dialog}
          className="clip-original-dialog"
          aria-labelledby={titleId}
          onCancel={(event) => {
            event.preventDefault();
            setOpened(false);
          }}
        >
          <header>
            <h2 id={titleId}>片段原文</h2>
            <button
              className="text-button"
              aria-label="关闭片段原文"
              onClick={() => setOpened(false)}
            >
              <Icon name="close" />
            </button>
          </header>
          <CandidateDetail
            key={active.id}
            candidate={active}
            transcriptUrl={transcriptUrl}
          />
        </dialog>
      )}
    </div>
  );
}
