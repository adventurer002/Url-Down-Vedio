import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useWorkspace } from "../hooks/useWorkspace";
import { useWorkspaceResults } from "../hooks/useWorkspaceResults";
import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import { terminal } from "../lib/taskTracker";
import type { JobKind } from "../types/api";
import { filesize, timecode } from "../utils";
import Icon from "../components/Icon";
import ErrorNotice from "../components/ErrorNotice";
import TaskCard from "../components/TaskCard";
import MindMapViewer from "../components/MindMapViewer";
export default function VideoWorkspaceView() {
  const store = useWorkspace(),
    { id = "" } = useParams();
  const video = store.videos.find((item) => item.key === id),
    jobs = store.tasks.filter((task) => task.videoKey === id);
  const [tab, setTab] = useState("content"),
    [raw, setRaw] = useState(false),
    [collapsed, setCollapsed] = useState(false),
    [thumbnailFailed, setThumbnailFailed] = useState(false);
  const [error, setError] = useState<ApiError | null>(null),
    [refreshingMetadata, setRefreshingMetadata] = useState(false);
  const { summary, transcript, mindmap, resultError, reloadResults } =
    useWorkspaceResults(id);
  function busy(kind: JobKind) {
    return (
      store.submitting.includes(`${video?.key}:${kind}`) ||
      jobs.some((task) => task.kind === kind && !terminal(task.event.status))
    );
  }
  async function create(kind: JobKind, label: string, presetId?: string) {
    if (!video) return;
    setError(null);
    try {
      await store.create(video, kind, label, presetId);
    } catch (e) {
      setError(normalizeError(e));
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
        <span className="workspace-tag">WORKSPACE</span>
      </div>
      <div className="workspace-heading">
        <div>
          <span className="eyebrow">MAKE IT YOURS</span>
          <h1>一个视频，无限收获。</h1>
        </div>
        <span className="workspace-heading-note">下载 · 阅读 · 理解</span>
      </div>
      <ErrorNotice error={error} />
      <ErrorNotice error={resultError}>
        <button className="text-button" onClick={reloadResults}>
          重新加载结果
        </button>
      </ErrorNotice>
      <nav className="mobile-tabs" aria-label="工作台分区">
        {[
          { id: "media", label: "视频与下载" },
          { id: "content", label: "文稿与总结" },
          { id: "mindmap", label: "思维导图" },
        ].map((item) => (
          <button
            key={item.id}
            className={tab === item.id ? "active" : ""}
            aria-pressed={tab === item.id}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>
      <div className={"workspace-grid" + (collapsed ? " map-collapsed" : "")}>
        <aside
          className={
            "workspace-panel media-panel" +
            (tab === "media" ? " mobile-active" : "")
          }
        >
          <div className="panel-heading">
            <span className="panel-number">01</span>
            <h2>视频与下载</h2>
            <Icon name="download" size={18} />
          </div>
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
                <Icon name="play" size={34} />
                <span>VIDEOHUB</span>
              </div>
            )}
            <span className="duration">
              {timecode(video.metadata.duration)}
            </span>
          </div>
          <div className="video-meta">
            {video.partial && (
              <button
                className="button secondary full"
                disabled={refreshingMetadata}
                onClick={refreshMetadata}
              >
                {refreshingMetadata ? "正在更新档位…" : "重新解析可用下载档位"}
              </button>
            )}
            <span className="platform-badge">{video.metadata.platform}</span>
            <h2>{video.metadata.title}</h2>
            <p>{video.metadata.uploader || "公开资源"}</p>
          </div>
          <div className="download-section">
            <div className="mini-heading">
              选择下载格式
              <span>{video.metadata.download_options.length} 个选项</span>
            </div>
            {video.metadata.download_options.map((option) => (
              <button
                key={option.id}
                className="format-button"
                disabled={busy("download")}
                onClick={() => void create("download", option.label, option.id)}
              >
                <span>
                  <strong>
                    {option.height
                      ? option.height + "P " + option.container.toUpperCase()
                      : "仅音频 MP3"}
                  </strong>
                  <small>{filesize(option.estimated_size_bytes)}</small>
                </span>
                <Icon name="download" size={17} />
              </button>
            ))}
            {!video.metadata.download_options.length && (
              <p className="muted">该视频暂无可下载的档位。</p>
            )}
            {taskCard("download")}
            <div className="audio-section">
              <span className="mini-heading">只想听声音？</span>
              <button
                className="button secondary full"
                disabled={busy("audio_extraction")}
                onClick={() => void create("audio_extraction", "纯音频 MP3")}
              >
                <Icon name="download" size={16} />
                提取纯音频 MP3
              </button>
              {taskCard("audio_extraction")}
            </div>
            <p className="panel-footnote">
              仅处理公开免登录资源。
              <br />
              请尊重创作者与内容版权。
            </p>
          </div>
        </aside>
        <section
          className={
            "workspace-panel content-panel" +
            (tab === "content" ? " mobile-active" : "")
          }
        >
          <div className="panel-heading">
            <span className="panel-number">02</span>
            <h2>文稿与总结</h2>
            <Icon name="text" size={18} />
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
                  <span className="model-label">
                    {summary.llm_provider === "heuristic"
                      ? "本地摘录模式"
                      : summary.llm_model}
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
                        <span className="timestamp">
                          [{timecode(chapter.start_seconds)}]
                        </span>
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
                        <div key={i}>
                          <time>{timecode(segment.start)}</time>
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
          className={
            "workspace-panel map-panel" +
            (tab === "mindmap" ? " mobile-active" : "")
          }
        >
          <div className="panel-heading">
            <span className="panel-number">03</span>
            <h2>思维导图</h2>
            <button
              className="icon-button collapse-map"
              aria-label={collapsed ? "展开思维导图" : "折叠思维导图"}
              onClick={() => setCollapsed(!collapsed)}
            >
              <Icon name={collapsed ? "plus" : "minus"} size={17} />
            </button>
          </div>
          <div
            className="map-content"
            style={{ display: collapsed ? "none" : undefined }}
          >
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
    </div>
  );
}
