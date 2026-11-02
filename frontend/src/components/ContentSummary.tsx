import { useState } from "react";
import type { SummaryResult } from "../types/api";
import type { Citation } from "../types/knowledge";
import { downloadText, timecode } from "../utils";
import Icon from "./Icon";

export default function ContentSummary({
  result,
  onCitation,
}: {
  result: SummaryResult;
  onCitation: (value: Citation) => void;
}) {
  const data = result.content_json;
  const [feedback, setFeedback] = useState(""),
    [copying, setCopying] = useState(false);
  const text = [
    data.overview,
    ...[data.conclusions, data.key_points, data.audience, data.action_steps]
      .filter(Boolean)
      .map((values) => values!.map((value) => `• ${value}`).join("\n")),
    ...data.chapters.map(
      (chapter) =>
        `${timecode(chapter.start_seconds)} ${chapter.title}\n${chapter.summary}`,
    ),
  ].join("\n\n");
  function jump(start: number, quote = "", end = start, id = 0) {
    onCitation({
      id,
      start,
      end,
      quote,
      approximate: data.source_basis === "asr",
    });
  }
  return (
    <article className="content-summary reading-content">
      <div className="content-reading-tools">
        <span>
          {data.source_basis === "asr"
            ? "基于语音转写整理"
            : "基于平台字幕整理"}
        </span>
        <div>
          <button
            className="text-button"
            disabled={copying}
            onClick={async () => {
              setCopying(true);
              try {
                await navigator.clipboard.writeText(text);
                setFeedback("总结已复制");
              } catch {
                setFeedback("复制失败，可下载总结后复制。");
              } finally {
                setCopying(false);
              }
            }}
          >
            <Icon name="copy" size={14} />
            {copying ? "正在复制…" : "复制总结"}
          </button>
          <button
            className="text-button"
            onClick={() =>
              downloadText(
                text,
                "videohub-summary.txt",
                "text/plain;charset=utf-8",
              )
            }
          >
            <Icon name="download" size={14} />
            导出总结
          </button>
        </div>
      </div>
      {feedback && (
        <p className="copy-feedback" role="status">
          {feedback}
        </p>
      )}
      <section className="reading-section summary-overview">
        <h3>核心概述</h3>
        <p className="overview-text">{data.overview}</p>
      </section>
      {(data.conclusions?.length || 0) > 0 && (
        <section className="reading-section">
          <h3>核心结论</h3>
          <ul className="summary-insights">
            {data.conclusions?.map((value, i) => (
              <li key={i}>{value}</li>
            ))}
          </ul>
        </section>
      )}
      <section className="reading-section">
        <h3>关键要点</h3>
        <ul className="summary-insights">
          {data.key_points.map((value, i) => (
            <li key={i}>{value}</li>
          ))}
        </ul>
      </section>
      {(data.evidence?.length || 0) > 0 && (
        <section className="reading-section">
          <h3>原文依据</h3>
          {data.evidence?.map((item, i) => (
            <blockquote className="summary-evidence" key={i}>
              <p>{item.quote}</p>
              <button
                className="text-button"
                onClick={() =>
                  jump(item.start_seconds, item.quote, item.end_seconds, i)
                }
              >
                {data.source_basis === "asr" ? "约 " : ""}
                {timecode(item.start_seconds)} · 查看文稿{" "}
                <Icon name="arrow" size={13} />
              </button>
            </blockquote>
          ))}
        </section>
      )}
      {(data.audience?.length || 0) > 0 && (
        <section className="reading-section">
          <h3>适合谁阅读</h3>
          <ul className="summary-insights">
            {data.audience?.map((value, i) => (
              <li key={i}>{value}</li>
            ))}
          </ul>
        </section>
      )}
      {(data.action_steps?.length || 0) > 0 && (
        <section className="reading-section">
          <h3>可以行动的步骤</h3>
          <ol className="summary-insights">
            {data.action_steps?.map((value, i) => (
              <li key={i}>{value}</li>
            ))}
          </ol>
        </section>
      )}
      <section className="reading-section summary-chapters">
        <h3>沿着时间，理解内容</h3>
        {data.chapters.map((chapter, i) => (
          <article className="chapter" key={i}>
            <button
              className="timestamp text-button"
              aria-label={`查看 ${timecode(chapter.start_seconds)} 对应文稿`}
              onClick={() => jump(chapter.start_seconds)}
            >
              {data.source_basis === "asr" ? "约 " : ""}
              {timecode(chapter.start_seconds)}
            </button>
            <div>
              <h4>{chapter.title}</h4>
              <p>{chapter.summary}</p>
            </div>
          </article>
        ))}
      </section>
    </article>
  );
}
