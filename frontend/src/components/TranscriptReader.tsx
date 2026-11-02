import { memo, useEffect, useMemo, useState } from "react";
import type { TranscriptResult } from "../types/api";
import type { Citation } from "../types/knowledge";
import { downloadText, timecode, transcriptToSrt } from "../utils";
import { originalLink } from "./VideoQuestions";

const PAGE_SIZE = 100;

function TranscriptReader({
  transcript,
  sourceUrl,
  focus,
  idPrefix = "transcript",
}: {
  transcript: TranscriptResult;
  sourceUrl: string;
  focus?: Citation;
  idPrefix?: string;
}) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [copying, setCopying] = useState(false);
  const [feedback, setFeedback] = useState("");
  const focusedIndex = useMemo(
    () =>
      focus
        ? transcript.segments.findIndex(
            (segment) =>
              (focus.quote && segment.text.includes(focus.quote)) ||
              (segment.start <= focus.start && segment.end >= focus.start),
          )
        : -1,
    [transcript, focus],
  );
  const matches = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return transcript.segments
      .map((segment, index) => ({ segment, index }))
      .filter(
        ({ segment }) =>
          !needle || segment.text.toLocaleLowerCase().includes(needle),
      );
  }, [transcript, query]);
  const currentPage = Math.min(
    page,
    Math.max(0, Math.ceil(matches.length / PAGE_SIZE) - 1),
  );
  const start = currentPage * PAGE_SIZE;
  const visible = matches.slice(start, start + PAGE_SIZE);
  const text =
    transcript.content_text ||
    transcript.segments.map((segment) => segment.text).join("\n");

  useEffect(() => {
    if (focusedIndex < 0) return;
    setQuery("");
    setPage(Math.floor(focusedIndex / PAGE_SIZE));
  }, [focusedIndex, focus]);
  useEffect(() => {
    if (
      focusedIndex < 0 ||
      query ||
      currentPage !== Math.floor(focusedIndex / PAGE_SIZE)
    )
      return;
    const frame = requestAnimationFrame(() =>
      document
        .getElementById(`${idPrefix}-segment-${focusedIndex}`)
        ?.scrollIntoView?.({
          behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)")
            .matches
            ? "auto"
            : "smooth",
          block: "center",
        }),
    );
    return () => cancelAnimationFrame(frame);
  }, [focusedIndex, focus, currentPage, query, idPrefix]);
  useEffect(() => {
    if (!feedback) return;
    const timer = setTimeout(() => setFeedback(""), 2500);
    return () => clearTimeout(timer);
  }, [feedback]);
  async function copy() {
    setCopying(true);
    setFeedback("");
    try {
      await navigator.clipboard.writeText(text);
      setFeedback("全文已复制");
    } catch {
      setFeedback("复制失败，可下载 TXT 文件后复制。");
    } finally {
      setCopying(false);
    }
  }
  return (
    <div className="transcript-reader">
      <div className="transcript-tools">
        <input
          type="search"
          aria-label="搜索文稿"
          placeholder="搜索文稿内容…"
          value={query}
          maxLength={200}
          onChange={(event) => {
            setQuery(event.target.value);
            setPage(0);
          }}
        />
        <div className="transcript-exports">
          <button
            className="text-button"
            disabled={copying || !text}
            onClick={() => void copy()}
          >
            {copying ? "正在复制…" : "复制全文"}
          </button>
          <button
            className="text-button"
            onClick={() =>
              downloadText(
                text,
                "videohub-transcript.txt",
                "text/plain;charset=utf-8",
              )
            }
          >
            下载 TXT
          </button>
          <button
            className="text-button"
            disabled={!transcript.segments.length}
            onClick={() =>
              downloadText(
                transcriptToSrt(transcript.segments),
                "videohub-transcript.srt",
                "application/x-subrip;charset=utf-8",
              )
            }
          >
            下载 SRT
          </button>
        </div>
      </div>
      {feedback && (
        <p className="muted" role="status">
          {feedback}
        </p>
      )}
      {query.trim() && (
        <p className="muted" role="status">
          找到 {matches.length} 段匹配文稿
        </p>
      )}
      <div className="transcript-segments">
        {visible.map(({ segment, index }) => (
          <div
            key={index}
            id={`${idPrefix}-segment-${index}`}
            className={index === focusedIndex ? "citation-highlight" : ""}
          >
            {originalLink(sourceUrl, segment.start) ? (
              <a
                href={originalLink(sourceUrl, segment.start)}
                target="_blank"
                rel="noreferrer"
              >
                <time>
                  {transcript.source_type === "asr" ? "约 " : ""}
                  {timecode(segment.start)}
                </time>
              </a>
            ) : (
              <time className="local-timestamp">
                {transcript.source_type === "asr" ? "约 " : ""}
                {timecode(segment.start)}
              </time>
            )}
            <p>{segment.text}</p>
          </div>
        ))}
      </div>
      {matches.length > PAGE_SIZE && (
        <nav className="transcript-pagination" aria-label="文稿分页">
          <button
            className="button secondary"
            disabled={currentPage === 0}
            onClick={() => setPage(currentPage - 1)}
          >
            上一页文稿
          </button>
          <span aria-live="polite">
            第 {start + 1}–{Math.min(start + PAGE_SIZE, matches.length)} 段，共{" "}
            {matches.length} 段
          </span>
          <button
            className="button secondary"
            disabled={start + PAGE_SIZE >= matches.length}
            onClick={() => setPage(currentPage + 1)}
          >
            下一页文稿
          </button>
        </nav>
      )}
    </div>
  );
}

export default memo(TranscriptReader);
