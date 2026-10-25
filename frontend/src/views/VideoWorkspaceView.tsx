import { useEffect, useRef, useState } from "react";
import { Link, useParams, useLocation } from "react-router-dom";
import { useWorkspace } from "../hooks/useWorkspace";
import { useWorkspaceResults } from "../hooks/useWorkspaceResults";
import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import { terminal } from "../lib/taskTracker";
import type { JobKind } from "../types/api";
import { timecode } from "../utils";
import Icon from "../components/Icon";
import ErrorNotice from "../components/ErrorNotice";
import TaskCard from "../components/TaskCard";
import MindMapViewer from "../components/MindMapViewer";
import DownloadSettings from "../components/DownloadSettings";
import AllowanceConfirm from "../components/AllowanceConfirm";
import { authStore, useAuth } from "../stores/auth";
import { billingStore, useBilling } from "../stores/billing";
import type { AllowanceQuote } from "../types/billing";
import { durationLabel } from "../utils/billing";
import VideoQuestions, { originalLink } from "../components/VideoQuestions";
import SaveToLibrary from "../components/SaveToLibrary";
import type { Citation } from "../types/knowledge";

export default function VideoWorkspaceView() {
  const store = useWorkspace(),
    { id = "" } = useParams();
  const video = store.videos.find((item) => item.key === id),
    jobs = store.tasks.filter((task) => task.videoKey === id);
  const [tab, setTab] = useState("media"),
    [raw, setRaw] = useState(false),
    [thumbnailFailed, setThumbnailFailed] = useState(false);
  const { search } = useLocation();
  const selectedTranscriptId = new URLSearchParams(search).get("transcript");
  useEffect(() => {
    if (new URLSearchParams(search).get("tab") === "questions")
      setTab("questions");
  }, [search]);
  const [error, setError] = useState<ApiError | null>(null),
    [refreshingMetadata, setRefreshingMetadata] = useState(false);
  const { summary, transcript, mindmap, resultError, reloadResults } =
    useWorkspaceResults(id);
  const { user } = useAuth();
  const billing = useBilling();
  const [proposal, setProposal] = useState<{
    quote: AllowanceQuote;
    kind: JobKind;
    label: string;
    presetId?: string;
    userId: string;
  }>();
  const [quoting, setQuoting] = useState<JobKind>();
  const [confirming, setConfirming] = useState(false);
  const [quoteError, setQuoteError] = useState<ApiError | null>(null);
  const request = useRef<AbortController | null>(null);
  const quoteGeneration = useRef(0);
  const confirmingRef = useRef(false);
  useEffect(() => {
    setProposal(undefined);
    setQuoting(undefined);
    setQuoteError(null);
    quoteGeneration.current++;
    request.current?.abort();
    return () => {
      quoteGeneration.current++;
      request.current?.abort();
    };
  }, [user?.id]);
  const [citationFocus, setCitationFocus] = useState<Citation>();
  function jumpToCitation(citation: Citation) {
    setRaw(true);
    setTab("content");
    setCitationFocus(citation);
  }
  useEffect(() => {
    if (!citationFocus || !transcript) return;
    const index = transcript.segments.findIndex(
      (segment) =>
        (citationFocus.quote && segment.text.includes(citationFocus.quote)) ||
        (segment.start <= citationFocus.start &&
          segment.end >= citationFocus.start),
    );
    const frame = requestAnimationFrame(() =>
      document
        .getElementById(`transcript-segment-${index}`)
        ?.scrollIntoView?.({ behavior: "smooth", block: "center" }),
    );
    return () => cancelAnimationFrame(frame);
  }, [citationFocus, transcript]);
  const [copying, setCopying] = useState(false);
  const [copyFeedback, setCopyFeedback] = useState<{ message: string } | null>(
    null,
  );
  useEffect(() => {
    if (!copyFeedback) return;
    const timer = window.setTimeout(() => setCopyFeedback(null), 2000);
    return () => window.clearTimeout(timer);
  }, [copyFeedback]);
  function busy(kind: JobKind) {
    return (
      store.submitting.includes(`${video?.key}:${kind}`) ||
      quoting === kind ||
      jobs.some((task) => task.kind === kind && !terminal(task.event.status))
    );
  }
  async function create(kind: JobKind, label: string, presetId?: string) {
    if (!video || !authStore.requireLogin()) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const expected = ++quoteGeneration.current;
    const userId = authStore.getState().user?.id || "";
    setError(null);
    setProposal(undefined);
    setQuoteError(null);
    setQuoting(kind);
    try {
      const quote = await api.quote(
        kind,
        video.url,
        presetId,
        controller.signal,
      );
      if (
        expected === quoteGeneration.current &&
        !controller.signal.aborted &&
        userId === (authStore.getState().user?.id || "")
      )
        setProposal({ quote, kind, label, presetId, userId });
    } catch (e) {
      if (expected === quoteGeneration.current && !controller.signal.aborted)
        setError(normalizeError(e));
    } finally {
      if (expected === quoteGeneration.current) setQuoting(undefined);
    }
  }
  async function confirm() {
    if (!proposal || !video || confirmingRef.current) return;
    if (proposal.userId !== (authStore.getState().user?.id || "")) {
      setProposal(undefined);
      return;
    }
    confirmingRef.current = true;
    setConfirming(true);
    setQuoteError(null);
    try {
      await store.create(
        video,
        proposal.kind,
        proposal.label,
        proposal.presetId,
        proposal.quote.quote_id,
      );
      setProposal(undefined);
      void billingStore.refresh();
    } catch (cause) {
      if (proposal.userId === (authStore.getState().user?.id || ""))
        setQuoteError(normalizeError(cause));
    } finally {
      confirmingRef.current = false;
      setConfirming(false);
    }
  }
  async function refreshMetadata() {
    if (!video || refreshingMetadata) return;
    setRefreshingMetadata(true);
    setError(null);
    try {
      store.remember(video.url, await api.parse(video.url));
    } catch (e) {
      setError(normalizeError(e));
    } finally {
      setRefreshingMetadata(false);
    }
  }
  const latest = (kind: JobKind) => jobs.find((task) => task.kind === kind);
  const taskCard = (kind: JobKind) => {
    const task = latest(kind);
    return task ? <TaskCard key={task.id} task={task} /> : null;
  };
  if (!video)
    return (
      <div className="missing-workspace">
        <span className="eyebrow">WORKSPACE NOT FOUND</span>
        <h1>重新连接你的内容。</h1>
        <p>此视频尚未在当前浏览器解析，或本地记录已清除。</p>
        <Link to="/" className="button primary">
          返回首页，粘贴视频链接 <Icon name="arrow" />
        </Link>
      </div>
    );
  return (
    <div className="workspace-page">
      <div className="workspace-breadcrumb">
        <Link to="/">首页</Link>
        <span>/</span>
        <span>视频工作台</span>
      </div>
      <div className="workspace-heading">
        <div>
          <h1>视频工作台</h1>
        </div>
        <Link to="/" className="button secondary new-video">
          <Icon name="plus-circle" size={22} />
          解析新视频
        </Link>
      </div>
      <ErrorNotice error={error} />
      {user && (
        <div className="workspace-quota-bar">
          <span>
            <Icon name="sparkles" size={15} />
            {billing.data ? (
              <>
                <b>
                  转写{" "}
                  {durationLabel(billing.data.balances.asr_seconds.available)}
                </b>
                <span>·</span>
                <b>AI {billing.data.balances.ai_generations.available} 次</b>
              </>
            ) : billing.error ? (
              "额度读取失败，可前往账户重试"
            ) : (
              "正在读取额度…"
            )}
          </span>
          <Link to="/subscription">
            订阅与额度
            <Icon name="arrow" size={13} />
          </Link>
        </div>
      )}
      <ErrorNotice error={resultError}>
        <button className="text-button" onClick={reloadResults}>
          重新加载结果
        </button>
      </ErrorNotice>
      <nav className="workspace-tabs" role="tablist" aria-label="工作台分区">
        {[
          { id: "media", label: "视频下载", icon: "download" },
          { id: "content", label: "文稿与总结", icon: "text" },
          { id: "mindmap", label: "思维导图", icon: "map" },
          { id: "questions", label: "视频问答", icon: "chat" },
        ].map((item, index, items) => (
          <button
            key={item.id}
            id={`tab-${item.id}`}
            role="tab"
            aria-selected={tab === item.id}
            aria-controls={`panel-${item.id}`}
            tabIndex={tab === item.id ? 0 : -1}
            onClick={() => setTab(item.id)}
            onKeyDown={(event) => {
              let next = index;
              if (event.key === "ArrowRight") next = (index + 1) % items.length;
              else if (event.key === "ArrowLeft")
                next = (index + items.length - 1) % items.length;
              else if (event.key === "Home") next = 0;
              else if (event.key === "End") next = items.length - 1;
              else return;
              event.preventDefault();
              setTab(items[next]!.id);
              document.getElementById(`tab-${items[next]!.id}`)?.focus();
            }}
          >
            <Icon name={item.icon} size={23} />
            {item.label}
          </button>
        ))}
      </nav>
      <div className="workspace-grid">
        <section
          className="workspace-panel media-panel"
          role="tabpanel"
          id="panel-media"
          aria-labelledby="tab-media"
          hidden={tab !== "media"}
        >
          <div className="download-panel-heading">
            <Icon name="download" size={30} />
            <div>
              <h2>视频下载</h2>
              <p>选择格式与清晰度，保存精彩内容。</p>
            </div>
            <span className="parse-status">
              <span />
              {video.partial ? "待更新档位" : "解析完成"}
            </span>
          </div>
          <div className="download-layout">
            <div className="video-preview-column">
              <div className="thumbnail">
                {video.metadata.thumbnail && !thumbnailFailed ? (
                  <img
                    src={video.metadata.thumbnail}
                    alt="视频封面"
                    referrerPolicy="no-referrer"
                    onError={() => setThumbnailFailed(true)}
                  />
                ) : (
                  <div className="thumbnail-placeholder">
                    <Icon name="play" size={40} />
                    <span>暂无视频封面</span>
                  </div>
                )}
                {/^https?:\/\//i.test(video.url) && (
                  <a
                    href={video.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="source-play"
                    aria-label="在原平台播放视频"
                  >
                    <Icon name="play" size={32} />
                  </a>
                )}
                <span className="duration">
                  {timecode(video.metadata.duration)}
                </span>
              </div>
              <div className="video-meta">
                <div className="video-title">
                  <h2>{video.metadata.title}</h2>
                  <p>{video.metadata.uploader || "公开资源"}</p>
                </div>
                {/^https?:\/\//i.test(video.url) && (
                  <a
                    className="source-link"
                    href={video.url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    查看原视频
                    <Icon name="external" size={17} />
                  </a>
                )}
              </div>
              {video.partial && (
                <button
                  className="button secondary full"
                  disabled={refreshingMetadata}
                  onClick={refreshMetadata}
                >
                  {refreshingMetadata
                    ? "正在更新档位…"
                    : "重新解析可用下载档位"}
                </button>
              )}
            </div>
            <div className="download-settings-column">
              <DownloadSettings
                options={video.metadata.download_options}
                downloading={busy("download")}
                extracting={busy("audio_extraction")}
                onCreate={(kind, label, presetId) =>
                  void create(kind, label, presetId)
                }
              />
              {taskCard("download")}
              {taskCard("audio_extraction")}
            </div>
          </div>
        </section>
        <section
          className="workspace-panel content-panel"
          role="tabpanel"
          id="panel-content"
          aria-labelledby="tab-content"
          hidden={tab !== "content"}
        >
          <div className="panel-heading">
            <Icon name="text" size={23} />
            <h2>文稿与总结</h2>
          </div>
          <div className="content-actions">
            <button
              className="button secondary"
              disabled={busy("transcription")}
              onClick={() => void create("transcription", "字幕 / 语音转写")}
            >
              {busy("transcription") ? "正在获取…" : "获取字幕 / 转写"}
            </button>
            <button
              className="button primary"
              disabled={busy("ai_summary")}
              onClick={() => void create("ai_summary", "AI 结构化总结")}
            >
              <span className="sparkle">✦</span>
              {busy("ai_summary") ? "正在总结…" : "生成 AI 总结"}
            </button>
          </div>
          <div className="content-tasks">
            {taskCard("transcription")}
            {taskCard("ai_summary")}
          </div>
          {transcript && (
            <div className="content-save-row">
              <SaveToLibrary
                transcriptId={transcript.id}
                resultId={summary?.id}
              />
            </div>
          )}
          {summary || transcript ? (
            <div className="reading-content">
              <div className="reading-heading">
                <span className="source-badge">
                  {(summary?.content_json.source_basis ||
                    transcript?.source_type) === "subtitle"
                    ? "基于平台字幕"
                    : "基于语音转写"}
                </span>
                {summary && (
                  <span className="summary-copy">
                    <button
                      className="model-copy-button"
                      title="复制完整总结"
                      disabled={copying}
                      onClick={async () => {
                        const c = summary.content_json;
                        const lines: string[] = [];
                        lines.push(c.overview || "");
                        for (const [heading, values] of [
                          ["核心结论", c.conclusions],
                          ["适合谁阅读", c.audience],
                          ["行动步骤", c.action_steps],
                        ] as const) {
                          if (values?.length)
                            lines.push(
                              "",
                              `【${heading}】`,
                              ...values.map(
                                (value, index) => `${index + 1}. ${value}`,
                              ),
                            );
                        }
                        if (c.evidence?.length)
                          lines.push(
                            "",
                            "【原文依据】",
                            ...c.evidence.map(
                              (item) =>
                                `[${c.source_basis === "asr" ? "约 " : ""}${timecode(item.start_seconds)}] ${item.quote}`,
                            ),
                          );
                        if (
                          Array.isArray(c.key_points) &&
                          c.key_points.length
                        ) {
                          lines.push("", "【要点】");
                          c.key_points.forEach((p: string, i: number) =>
                            lines.push(`${i + 1}. ${p}`),
                          );
                        }
                        if (Array.isArray(c.chapters) && c.chapters.length) {
                          lines.push("", "【章节】");
                          c.chapters.forEach(
                            (ch: {
                              title: string;
                              summary: string;
                              start_seconds: number;
                            }) =>
                              lines.push(
                                `[${timecode(ch.start_seconds)}] ${ch.title}\n${ch.summary}`,
                              ),
                          );
                        }
                        setCopying(true);
                        setCopyFeedback(null);
                        try {
                          await navigator.clipboard.writeText(lines.join("\n"));
                          setCopyFeedback({ message: "复制成功" });
                        } catch {
                          setCopyFeedback({ message: "复制失败，请重试" });
                        } finally {
                          setCopying(false);
                        }
                      }}
                    >
                      <Icon name="copy" size={12} />
                      复制总结
                    </button>
                    {copyFeedback && (
                      <span className="copy-feedback" role="status">
                        {copyFeedback.message}
                      </span>
                    )}
                  </span>
                )}
              </div>
              {summary && (
                <>
                  <div className="reading-section">
                    <span className="eyebrow">THE BIG PICTURE</span>
                    <h3>核心概述</h3>
                    <p className="overview-text">
                      {summary.content_json.overview}
                    </p>
                  </div>
                  {(summary.content_json.conclusions?.length || 0) > 0 && (
                    <div className="reading-section">
                      <h3>核心结论</h3>
                      <ul className="summary-insights">
                        {summary.content_json.conclusions?.map(
                          (value, index) => (
                            <li key={index}>{value}</li>
                          ),
                        )}
                      </ul>
                    </div>
                  )}
                  {(summary.content_json.evidence?.length || 0) > 0 && (
                    <div className="reading-section">
                      <h3>原文依据</h3>
                      {summary.content_json.evidence?.map((item, index) => (
                        <blockquote className="summary-evidence" key={index}>
                          <p>{item.quote}</p>
                          <button
                            className="text-button"
                            onClick={() =>
                              jumpToCitation({
                                id: index,
                                start: item.start_seconds,
                                end: item.end_seconds,
                                quote: item.quote,
                                approximate:
                                  summary.content_json.source_basis === "asr",
                              })
                            }
                          >
                            {summary.content_json.source_basis === "asr"
                              ? "约 "
                              : ""}
                            {timecode(item.start_seconds)} · 查看文稿
                          </button>
                        </blockquote>
                      ))}
                    </div>
                  )}
                  {(summary.content_json.audience?.length || 0) > 0 && (
                    <div className="reading-section">
                      <h3>适合谁阅读</h3>
                      <ul className="summary-insights">
                        {summary.content_json.audience?.map((value, index) => (
                          <li key={index}>{value}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {(summary.content_json.action_steps?.length || 0) > 0 && (
                    <div className="reading-section">
                      <h3>可以行动的步骤</h3>
                      <ol className="summary-insights">
                        {summary.content_json.action_steps?.map(
                          (value, index) => (
                            <li key={index}>{value}</li>
                          ),
                        )}
                      </ol>
                    </div>
                  )}
                  <div className="reading-section">
                    <h3>值得记住的要点</h3>
                    <ol className="key-points">
                      {summary.content_json.key_points.map((point, i) => (
                        <li key={i}>
                          <span>{String(i + 1).padStart(2, "0")}</span>
                          <p>{point}</p>
                        </li>
                      ))}
                    </ol>
                  </div>
                  <div className="reading-section">
                    <h3>沿着时间，理解内容</h3>
                    {summary.content_json.chapters.map((chapter, i) => (
                      <article key={i} className="chapter">
                        <button
                          className="timestamp text-button"
                          aria-label={`查看 ${timecode(chapter.start_seconds)} 对应文稿`}
                          onClick={() =>
                            jumpToCitation({
                              id: i,
                              start: chapter.start_seconds,
                              end: chapter.start_seconds,
                              quote: "",
                              approximate:
                                summary.content_json.source_basis === "asr",
                            })
                          }
                        >
                          [
                          {summary.content_json.source_basis === "asr"
                            ? "约 "
                            : ""}
                          {timecode(chapter.start_seconds)}]
                        </button>
                        <div>
                          <h4>{chapter.title}</h4>
                          <p>{chapter.summary}</p>
                        </div>
                      </article>
                    ))}
                  </div>
                  <p className="usage-line">
                    {summary.input_tokens == null
                      ? "Token 用量未提供"
                      : `${summary.input_tokens} 输入 / ${summary.output_tokens ?? "—"} 输出 Token`}
                    {summary.duration_ms != null && (
                      <span>
                        {" "}
                        · {(summary.duration_ms / 1000).toFixed(1)} 秒
                      </span>
                    )}
                  </p>
                </>
              )}
              {transcript && (
                <div className="transcript-section">
                  <button
                    className="transcript-toggle"
                    onClick={() => setRaw(!raw)}
                    aria-expanded={raw}
                  >
                    <span>
                      {summary ? "查看原始文稿" : "原始字幕文稿"}
                      <small>
                        {transcript.language.toUpperCase()} ·{" "}
                        {transcript.segments.length} 段
                      </small>
                    </span>
                    <Icon name={raw ? "minus" : "plus"} size={18} />
                  </button>
                  {(raw || !summary) && (
                    <div className="transcript-segments">
                      {transcript.segments.map((segment, i) => (
                        <div
                          key={i}
                          id={`transcript-segment-${i}`}
                          className={
                            citationFocus &&
                            ((citationFocus.quote &&
                              segment.text.includes(citationFocus.quote)) ||
                              (segment.start <= citationFocus.start &&
                                segment.end >= citationFocus.start))
                              ? "citation-highlight"
                              : ""
                          }
                        >
                          <a
                            href={originalLink(video.url, segment.start)}
                            target="_blank"
                            rel="noreferrer"
                          >
                            <time>
                              {transcript.source_type === "asr" ? "约 " : ""}
                              {timecode(segment.start)}
                            </time>
                          </a>
                          <p>{segment.text}</p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ) : (
            <div className="empty-state content-empty">
              <span className="empty-icon">
                <Icon name="text" size={30} />
              </span>
              <h3>精彩内容，值得细读。</h3>
              <p>
                获取字幕，或让 AI 为你提炼要点。
                <br />
                长视频，也能一目了然。
              </p>
              <div className="empty-preview">
                <div />
                <div />
                <div />
              </div>
            </div>
          )}
        </section>
        <section
          className="workspace-panel questions-panel"
          role="tabpanel"
          id="panel-questions"
          aria-labelledby="tab-questions"
          hidden={tab !== "questions"}
        >
          <div className="panel-heading">
            <Icon name="chat" size={23} />
            <h2>视频问答</h2>
          </div>
          {transcript || selectedTranscriptId ? (
            <VideoQuestions
              video={video}
              transcriptId={selectedTranscriptId || transcript!.id}
              onJump={
                !selectedTranscriptId || selectedTranscriptId === transcript?.id
                  ? jumpToCitation
                  : undefined
              }
            />
          ) : (
            <div className="empty-state">
              <Icon name="chat" size={32} />
              <h3>先有文稿，再聊深入一点。</h3>
              <p>获取字幕或完成总结后，就可以针对这段视频提问。</p>
              <button
                className="button secondary"
                onClick={() => setTab("content")}
              >
                前往获取文稿
              </button>
            </div>
          )}
        </section>
        <section
          className="workspace-panel map-panel"
          role="tabpanel"
          id="panel-mindmap"
          aria-labelledby="tab-mindmap"
          hidden={tab !== "mindmap"}
        >
          <div className="panel-heading">
            <Icon name="map" size={23} />
            <h2>思维导图</h2>
          </div>
          <div className="map-content">
            <div className="map-intro">
              <p>把内容连成一张知识地图。</p>
              <button
                className="button primary full"
                disabled={busy("ai_mindmap")}
                onClick={() => void create("ai_mindmap", "AI 思维导图")}
              >
                <Icon name="map" size={16} />
                {busy("ai_mindmap")
                  ? "正在构建…"
                  : mindmap
                    ? "重新生成思维导图"
                    : "生成思维导图"}
              </button>
            </div>
            {taskCard("ai_mindmap")}
            {mindmap ? (
              <MindMapViewer result={mindmap} />
            ) : (
              <div className="empty-state map-empty">
                <div className="map-preview" aria-hidden="true">
                  <div className="map-root">核心主题</div>
                  <div className="map-branches">
                    <span>观点一</span>
                    <span>观点二</span>
                    <span>观点三</span>
                  </div>
                </div>
                <h3>看见知识的形状。</h3>
                <p>
                  从主题到细节，
                  <br />
                  在可交互的导图中探索关联。
                </p>
              </div>
            )}
          </div>
        </section>
      </div>
      {proposal && (
        <AllowanceConfirm
          quote={proposal.quote}
          label={proposal.label}
          pending={confirming}
          error={quoteError}
          onConfirm={() => void confirm()}
          onClose={() => {
            if (!confirmingRef.current) setProposal(undefined);
          }}
          onRefresh={() =>
            void create(proposal.kind, proposal.label, proposal.presetId)
          }
        />
      )}
    </div>
  );
}
