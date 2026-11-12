import { useEffect, useRef, useState } from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useWorkspace } from "../hooks/useWorkspace";
import { useWorkspaceResults } from "../hooks/useWorkspaceResults";
import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import { terminal } from "../lib/taskTracker";
import { processingInput, sourceExpired, sourceLabel } from "../lib/content";
import type { JobKind } from "../types/api";
import { filesize, timecode } from "../utils";
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
import VideoQuestions from "../components/VideoQuestions";
import TranscriptReader from "../components/TranscriptReader";
import SaveToLibrary from "../components/SaveToLibrary";
import type { Citation } from "../types/knowledge";
import UploadStatus from "../components/UploadStatus";
import ContentSummary from "../components/ContentSummary";
import CitationDrawer from "../components/CitationDrawer";

import { useClipWorkspace } from "../hooks/useClipWorkspace";
import { clipsCreationEnabled, isClipTask } from "../lib/clips";
import ClipWorkspace from "../components/clips/ClipWorkspace";
import "../clips.css";

const baseTabs = [
  { id: "transcript", label: "文稿", short: "文稿", icon: "text" },
  { id: "summary", label: "AI 总结", short: "总结", icon: "sparkles" },
  { id: "mindmap", label: "思维导图", short: "导图", icon: "map" },
  { id: "questions", label: "内容问答", short: "问答", icon: "chat" },
] as const;
export default function VideoWorkspaceView() {
  const store = useWorkspace(),
    { id = "" } = useParams(),
    video = store.findContent(id);
  const key = video?.key || id,
    jobs = store.tasks.filter((task) => task.videoKey === key);
  const [params, setParams] = useSearchParams(),
    requested = params.get("tab");
  const navigate = useNavigate();
  const hasClipRef = !!(
    params.get("clip_plan") ||
    params.get("clip_export") ||
    params.get("clip_task")
  );
  const showClips =
    clipsCreationEnabled() ||
    hasClipRef ||
    requested === "clips" ||
    !!video?.clipPlanId ||
    jobs.some((task) => isClipTask(task.kind));
  const tabs = [
    ...baseTabs,
    ...(showClips
      ? [{ id: "clips", label: "精彩切片", short: "切片", icon: "clips" }]
      : []),
  ];
  const clip = useClipWorkspace(
    key,
    {
      plan: params.get("clip_plan"),
      exported: params.get("clip_export"),
      task: params.get("clip_task"),
    },
    showClips && (requested === "clips" || hasClipRef),
  );
  useEffect(() => {
    if (!video && clip.contentKey && !clip.error)
      navigate(`/video/${encodeURIComponent(clip.contentKey)}?${params}`, {
        replace: true,
      });
  }, [video, clip.contentKey, clip.error, navigate, params]);
  const tab =
    requested === "content"
      ? "summary"
      : tabs.some((item) => item.id === requested)
        ? requested!
        : "transcript";
  const result = useWorkspaceResults(
    key,
    params.get("transcript"),
    params.get("result"),
  );
  const { summary, transcript, mindmap, resultError } = result;
  const { user, restoring, restoreError } = useAuth(),
    billing = useBilling();
  const [downloadOpen, setDownloadOpen] = useState(
    () =>
      video?.source?.kind !== "upload" && (!requested || requested === "media"),
  );
  const [error, setError] = useState<ApiError | null>(null),
    [refreshing, setRefreshing] = useState(false);
  const [citation, setCitation] = useState<Citation>(),
    [clock, setClock] = useState(Date.now());
  const [proposal, setProposal] = useState<{
    quote: AllowanceQuote;
    kind: JobKind;
    label: string;
    presetId?: string;
    owner: string;
    text: boolean;
  }>();
  const [quoting, setQuoting] = useState<JobKind>(),
    [confirming, setConfirming] = useState(false),
    [quoteError, setQuoteError] = useState<ApiError | null>(null);
  const [deleting, setDeleting] = useState(false),
    [deleteOpen, setDeleteOpen] = useState(false),
    [deleteError, setDeleteError] = useState<ApiError | null>(null);
  const deleteDialog = useRef<HTMLDialogElement>(null),
    request = useRef<AbortController | null>(null),
    generation = useRef(0),
    lock = useRef(false),
    alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    const timer = setInterval(() => setClock(Date.now()), 30000);
    return () => {
      alive.current = false;
      clearInterval(timer);
      request.current?.abort();
    };
  }, []);
  useEffect(() => {
    generation.current++;
    request.current?.abort();
    setProposal(undefined);
    setQuoting(undefined);
    setQuoteError(null);
    setCitation(undefined);
  }, [key, user?.id, transcript?.id]);
  useEffect(() => {
    if (!deleteOpen) return;
    const previous = document.activeElement as HTMLElement | null;
    deleteDialog.current?.showModal();
    return () => {
      deleteDialog.current?.close();
      if (previous?.isConnected) previous.focus();
    };
  }, [deleteOpen]);
  function setTab(value: string) {
    setCitation(undefined);
    setDownloadOpen(false);
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        next.set("tab", value);
        if (value === "transcript" && tab === "clips" && clip.plan)
          next.set("transcript", clip.plan.transcript_id);
        return next;
      },
      { replace: true },
    );
  }
  const uploaded = video?.source?.kind === "upload",
    expired = video ? sourceExpired(video, clock) : false;
  const transfer = uploaded
    ? store.uploads.entries.find(
        (entry) => entry.id === video?.source?.uploadId,
      )
    : undefined;
  const mediaReady =
    !uploaded ||
    (!!video.source?.uploadId && video.source.status === "ready" && !expired);
  const contentActive = jobs.some(
    (task) =>
      [
        "transcription",
        "ai_summary",
        "ai_mindmap",
        "media_probe",
        "ai_clip_plan",
        "clips_export",
      ].includes(task.kind) && !terminal(task.event.status),
  );
  function busy(kind: JobKind) {
    return (
      quoting === kind ||
      store.submitting.includes(`${key}:${kind}`) ||
      jobs.some((task) => task.kind === kind && !terminal(task.event.status))
    );
  }
  async function create(kind: JobKind, label: string, presetId?: string) {
    if (!video || !authStore.requireLogin()) return;
    request.current?.abort();
    const controller = new AbortController(),
      expected = ++generation.current,
      owner = authStore.getState().user?.id || "";
    request.current = controller;
    setError(null);
    setQuoteError(null);
    setProposal(undefined);
    setQuoting(kind);
    try {
      const input = processingInput(video, kind, transcript?.id);
      const quote = await api.quote(
        kind,
        input.url ?? input,
        presetId,
        controller.signal,
      );
      if (
        expected === generation.current &&
        !controller.signal.aborted &&
        owner === (authStore.getState().user?.id || "")
      )
        setProposal({
          quote,
          kind,
          label,
          presetId,
          owner,
          text: !!input.transcript_id,
        });
    } catch (cause) {
      if (expected === generation.current && !controller.signal.aborted)
        setError(normalizeError(cause));
    } finally {
      if (expected === generation.current) setQuoting(undefined);
    }
  }
  async function confirm() {
    if (
      !video ||
      !proposal ||
      lock.current ||
      proposal.owner !== (authStore.getState().user?.id || "")
    )
      return;
    lock.current = true;
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
      if (
        alive.current &&
        proposal.owner === (authStore.getState().user?.id || "")
      ) {
        const nextTab = (
          {
            ai_summary: "summary",
            ai_mindmap: "mindmap",
            transcription: "transcript",
          } as Record<string, string>
        )[proposal.kind];
        if (nextTab)
          setParams(
            (previous) => {
              const next = new URLSearchParams(previous);
              next.set("tab", nextTab);
              next.delete("result");
              return next;
            },
            { replace: true },
          );
        setProposal(undefined);
        void billingStore.refresh();
      }
    } catch (cause) {
      if (
        alive.current &&
        proposal.owner === (authStore.getState().user?.id || "")
      )
        setQuoteError(normalizeError(cause));
    } finally {
      lock.current = false;
      if (alive.current) setConfirming(false);
    }
  }
  async function refreshMetadata() {
    if (!video || refreshing) return;
    setRefreshing(true);
    setError(null);
    request.current?.abort();
    const controller = new AbortController();
    const owner = authStore.getState().user?.id;
    request.current = controller;
    try {
      const metadata = await api.parse(video.url, controller.signal);
      if (
        alive.current &&
        !controller.signal.aborted &&
        owner === authStore.getState().user?.id
      )
        store.remember(video.url, metadata);
    } catch (cause) {
      if (alive.current && !controller.signal.aborted)
        setError(normalizeError(cause));
    } finally {
      if (alive.current && request.current === controller) setRefreshing(false);
    }
  }
  if (!video && hasClipRef)
    return (
      <div className="missing-workspace">
        <h1>
          {restoring || clip.loading || store.historyLoading
            ? "正在恢复切片方案…"
            : user
              ? "切片任务与候选"
              : "登录后查看切片方案"}
        </h1>
        <ErrorNotice error={clip.error}>
          <button className="text-button" onClick={clip.reload}>
            重新加载方案
          </button>
        </ErrorNotice>
        {restoreError && <p>请先恢复账户连接。</p>}
        {clip.task && <TaskCard task={clip.task} />}
        {!user && !restoring && (
          <button
            className="button primary"
            onClick={() => authStore.requireLogin()}
          >
            登录
          </button>
        )}
        <div className="workspace-empty-actions">
          <button
            className="button secondary"
            onClick={() => store.setHistoryOpen(true)}
          >
            打开任务历史
          </button>
          <Link className="text-button" to="/app">
            返回工作台
          </Link>
        </div>
      </div>
    );
  if (!video)
    return (
      <div className="missing-workspace">
        <h1>
          {restoring || store.historyLoading
            ? "正在恢复内容工作台…"
            : "暂时找不到这份内容"}
        </h1>
        <p>
          {restoreError
            ? "请先恢复账户连接。"
            : "可以从任务历史重新打开，或导入一份新内容。"}
        </p>
        <div className="workspace-empty-actions">
          <Link className="button primary" to="/app">
            导入新内容
          </Link>
          <button
            className="button secondary"
            onClick={() => store.setHistoryOpen(true)}
          >
            打开任务历史
          </button>
        </div>
      </div>
    );
  const canAI = !!transcript || mediaReady;
  const activeJobs = jobs.filter(
    (task) =>
      task.kind !== "ai_question" &&
      (!terminal(task.event.status) ||
        (task.event.status === "failed" &&
          jobs.find((other) => other.kind === task.kind)?.id === task.id)),
  );
  const anyReadyResult = !!(transcript || summary || mindmap);
  const fileStatus =
    uploaded && video.source?.status === "deleted"
      ? "原文件已删除"
      : expired
        ? "原文件已到期"
        : undefined;
  const idleActions = (
    <div className="content-goals" aria-label="选择内容处理方式">
      <button
        className="content-goal"
        disabled={!mediaReady || contentActive || !!quoting}
        onClick={() => void create("transcription", "提取文稿")}
      >
        <Icon name="text" size={22} />
        <strong>只提取文稿</strong>
        <span>阅读、搜索与导出原文</span>
      </button>
      <button
        className="content-goal"
        disabled={!canAI || contentActive || !!quoting}
        onClick={() => void create("ai_summary", "生成 AI 总结")}
      >
        <Icon name="sparkles" size={22} />
        <strong>生成总结</strong>
        <span>从文稿中梳理结论与要点</span>
      </button>
      <button
        className="content-goal featured"
        disabled={!canAI || contentActive || !!quoting}
        onClick={() => void create("ai_mindmap", "生成总结与思维导图")}
      >
        <Icon name="map" size={22} />
        <strong>生成总结与导图</strong>
        <span>一次整理，同时获得两种成果</span>
        <span className="goal-corner">
          <Icon name="arrow" size={16} />
        </span>
      </button>
    </div>
  );
  return (
    <div className="workspace-page content-workspace">
      <div className="workspace-breadcrumb">
        <Link to="/app">工作台</Link>
        <span>/</span>
        <span>内容工作台</span>
      </div>
      <section aria-label="当前内容">
        <header className="content-context">
          <span className="content-source-icon">
            <Icon
              name={
                uploaded
                  ? video.source?.mediaType === "audio"
                    ? "audio"
                    : "play"
                  : "link"
              }
              size={27}
            />
          </span>
          <div className="content-context-copy">
            <div className="content-source-type">
              {sourceLabel(video)}
              {video.metadata.uploader && (
                <span>{video.metadata.uploader}</span>
              )}
            </div>
            <h1>{video.metadata.title}</h1>
            <div className="content-meta">
              <span>
                <Icon name="clock" size={14} />
                {video.metadata.duration == null
                  ? "时长待校验"
                  : timecode(video.metadata.duration)}
              </span>
              {video.source?.sizeBytes != null && (
                <span>{filesize(video.source.sizeBytes)}</span>
              )}
              {uploaded && video.source?.expiresAt && !fileStatus && (
                <span>
                  原文件保留至{" "}
                  {new Intl.DateTimeFormat("zh-CN", {
                    timeZone: "Asia/Shanghai",
                    month: "numeric",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  }).format(new Date(video.source.expiresAt))}
                </span>
              )}
            </div>
          </div>
          <Link className="button secondary new-content" to="/app">
            <Icon name="plus" size={17} />
            导入新内容
          </Link>
        </header>
        <div className="content-context-actions">
          {!uploaded && (
            <>
              <button
                className="button secondary"
                aria-expanded={downloadOpen}
                onClick={() => setDownloadOpen(!downloadOpen)}
              >
                <Icon name="download" size={16} />
                下载视频 / 提取音频
              </button>
              <a
                className="text-button"
                href={video.url}
                target="_blank"
                rel="noopener noreferrer"
              >
                查看原视频 <Icon name="external" size={14} />
              </a>
            </>
          )}
          {uploaded && video.source?.uploadId && !fileStatus && (
            <button
              className="text-button source-delete"
              onClick={() => {
                setDeleteError(null);
                setDeleteOpen(true);
              }}
            >
              <Icon name="trash" size={14} />
              删除原文件
            </button>
          )}
          {transcript && (
            <SaveToLibrary
              transcriptId={transcript.id}
              resultId={summary?.id}
            />
          )}
        </div>
      </section>
      {fileStatus && (
        <p className="source-retention-note" role="status">
          <Icon name="text" size={18} />
          <span>
            <strong>{fileStatus}。</strong>文稿和已获得的 AI
            结果仍可继续使用，生成总结或导图无需重新上传。
          </span>
          <Link to="/app">重新上传</Link>
        </p>
      )}
      {uploaded &&
        transfer &&
        !["ready", "cancelled"].includes(transfer.phase) && (
          <UploadStatus entry={transfer} />
        )}
      {uploaded && transfer?.phase === "ready" && !expired && (
        <LocalPreview
          id={transfer.id}
          audio={video.source?.mediaType === "audio"}
        />
      )}
      <ErrorNotice error={error} />
      {!uploaded && (
        <section
          className="content-download"
          hidden={!downloadOpen}
          aria-label="视频下载"
        >
          <div className="content-download-heading">
            <div>
              <h2>视频下载</h2>
              <p>选择格式与清晰度，保存精彩内容。</p>
            </div>
            <button
              className="text-button"
              onClick={() => setDownloadOpen(false)}
            >
              收起 <Icon name="minus" size={15} />
            </button>
          </div>
          <div className="content-download-body">
            <div className="content-download-media">
              <div className="content-download-preview">
                {video.metadata.thumbnail ? (
                  <img
                    src={video.metadata.thumbnail}
                    alt="视频封面"
                    referrerPolicy="no-referrer"
                  />
                ) : (
                  <p className="muted">暂无封面</p>
                )}
                <span>{timecode(video.metadata.duration)}</span>
              </div>
              <div className="content-download-info">
                <div>
                  <p className="content-download-title">
                    {video.metadata.title}
                  </p>
                  {video.metadata.uploader && <p>{video.metadata.uploader}</p>}
                </div>
                <a href={video.url} target="_blank" rel="noopener noreferrer">
                  查看原视频
                </a>
              </div>
            </div>
            <div className="content-download-controls">
              {video.partial && (
                <button
                  className="button secondary"
                  disabled={refreshing}
                  onClick={() => void refreshMetadata()}
                >
                  {refreshing ? "正在更新档位…" : "重新解析可用下载档位"}
                </button>
              )}
              <DownloadSettings
                options={video.metadata.download_options}
                downloading={busy("download")}
                extracting={busy("audio_extraction")}
                onCreate={(kind, label, presetId) =>
                  void create(kind, label, presetId)
                }
              />
              {jobs
                .filter((task) =>
                  ["download", "audio_extraction"].includes(task.kind),
                )
                .slice(0, 2)
                .map((task) => (
                  <TaskCard key={task.id} task={task} />
                ))}
            </div>
          </div>
        </section>
      )}
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
                <span> · </span>
                <b>AI {billing.data.balances.ai_generations.available} 次</b>
              </>
            ) : billing.error ? (
              "额度暂时无法读取"
            ) : (
              "正在读取额度…"
            )}
          </span>
          <Link to="/account">
            查看额度 <Icon name="arrow" size={13} />
          </Link>
        </div>
      )}
      {activeJobs
        .filter(
          (task) =>
            !["download", "audio_extraction"].includes(task.kind) &&
            (tab !== "clips" || !isClipTask(task.kind)),
        )
        .map((task) => (
          <TaskCard key={task.id} task={task} />
        ))}
      <ErrorNotice error={resultError}>
        <button className="text-button" onClick={result.reloadResults}>
          重新加载结果
        </button>
      </ErrorNotice>
      {result.hasNewResults && (
        <div className="new-result-notice" role="status">
          <span>
            <Icon name="check" size={17} />
            新结果已生成，当前阅读位置已保留。
          </span>
          <button
            className="text-button"
            onClick={() => {
              result.acceptNewResults();
              setCitation(undefined);
            }}
          >
            查看新结果 <Icon name="arrow" size={14} />
          </button>
        </div>
      )}
      <div
        className={`content-reading-layout${citation ? " with-citation" : ""}`}
      >
        <section className="content-main">
          <nav
            className={`workspace-tabs content-tabs${showClips ? " clip-enabled-tabs" : ""}`}
            role="tablist"
            aria-label="工作台分区"
          >
            {tabs.map((item, index) => (
              <button
                key={item.id}
                id={`tab-${item.id}`}
                role="tab"
                aria-label={item.label}
                aria-selected={tab === item.id}
                aria-controls={`panel-${item.id}`}
                tabIndex={tab === item.id ? 0 : -1}
                onClick={() => setTab(item.id)}
                onKeyDown={(event) => {
                  let next = index;
                  if (event.key === "ArrowRight")
                    next = (index + 1) % tabs.length;
                  else if (event.key === "ArrowLeft")
                    next = (index + tabs.length - 1) % tabs.length;
                  else if (event.key === "Home") next = 0;
                  else if (event.key === "End") next = tabs.length - 1;
                  else return;
                  event.preventDefault();
                  setTab(tabs[next].id);
                  document.getElementById(`tab-${tabs[next].id}`)?.focus();
                }}
              >
                <Icon name={item.icon} size={18} />
                <span className="tab-full-label">{item.label}</span>
                <span className="tab-short-label">{item.short}</span>
                {(item.id === "transcript"
                  ? transcript
                  : item.id === "summary"
                    ? summary
                    : item.id === "mindmap"
                      ? mindmap
                      : item.id === "clips"
                        ? clip.plan || video.clipPlanId
                        : null) && (
                  <span className="tab-ready-dot" aria-label="已有结果" />
                )}
              </button>
            ))}
          </nav>
          <section
            role="tabpanel"
            id="panel-transcript"
            aria-labelledby="tab-transcript"
            hidden={tab !== "transcript"}
            className="content-panel"
          >
            <div className="content-panel-heading">
              <div>
                <h2>原始文稿</h2>
                <p>
                  {transcript
                    ? `${transcript.source_type === "asr" ? "语音转写" : "平台字幕"} · ${transcript.language.toUpperCase()} · ${transcript.segments.length} 段`
                    : "从原文出发，找到内容里的细节。"}
                </p>
              </div>
              {transcript && (
                <button
                  className="button secondary"
                  disabled={contentActive || !!quoting}
                  onClick={() =>
                    summary
                      ? setTab("summary")
                      : void create("ai_summary", "生成 AI 总结")
                  }
                >
                  <Icon name="sparkles" size={16} />
                  {summary ? "查看 AI 总结" : "生成 AI 总结"}
                </button>
              )}
            </div>
            {transcript ? (
              <div className="content-transcript-reading">
                <TranscriptReader
                  transcript={transcript}
                  sourceUrl={video.url}
                />
              </div>
            ) : !contentActive && mediaReady ? (
              <section className="content-start">
                <h3>接下来，想如何整理？</h3>
                <p>选择需要的成果，开始前会显示本次额度。</p>
                {idleActions}
                {clipsCreationEnabled() && (
                  <button
                    className="text-button clip-entry"
                    onClick={() => setTab("clips")}
                  >
                    <Icon name="clips" size={17} />
                    寻找精彩片段
                  </button>
                )}
              </section>
            ) : (
              <EmptyPanel
                icon="text"
                title={
                  contentActive
                    ? "正在准备文稿"
                    : expired
                      ? "还没有可用的文稿"
                      : "原文，值得细读"
                }
                description={
                  contentActive
                    ? "处理完成后，文稿会出现在这里。你也可以稍后从任务历史继续。"
                    : expired
                      ? "请重新上传原文件，提取文稿后即可持续阅读和整理。"
                      : "提取字幕或识别语音，获得带时间范围的文字。"
                }
              >
                {!contentActive && mediaReady && anyReadyResult && (
                  <button
                    className="button secondary"
                    onClick={() => void create("transcription", "提取文稿")}
                  >
                    提取文稿
                  </button>
                )}
              </EmptyPanel>
            )}
          </section>
          <section
            role="tabpanel"
            id="panel-summary"
            aria-labelledby="tab-summary"
            hidden={tab !== "summary"}
            className="content-panel content-panel-scrollable"
          >
            <div className="content-panel-heading">
              <div>
                <h2>AI 结构化总结</h2>
                <p>
                  {transcript
                    ? "使用已有文稿，无需重新转写。"
                    : "梳理核心结论、关键要点与原文依据。"}
                </p>
              </div>
              {summary && (
                <button
                  className="text-button"
                  disabled={contentActive || !!quoting}
                  onClick={() => void create("ai_summary", "获取 AI 总结")}
                >
                  重新获取
                </button>
              )}
            </div>
            <div
              className="content-panel-body"
              role="region"
              aria-label="总结内容"
              tabIndex={0}
            >
              {summary ? (
                <ContentSummary result={summary} onCitation={setCitation} />
              ) : (
                <EmptyPanel
                  icon="sparkles"
                  title={
                    busy("ai_summary") ? "正在梳理内容要点" : "读懂内容里的关键"
                  }
                  description={
                    busy("ai_summary")
                      ? "生成完成后可阅读总结，并从引用回到原文。"
                      : "提取核心结论与章节脉络，每个要点都能回到文稿核对。"
                  }
                >
                  <button
                    className="button primary"
                    disabled={!canAI || contentActive || !!quoting}
                    onClick={() => void create("ai_summary", "生成 AI 总结")}
                  >
                    {busy("ai_summary") ? "正在总结…" : "生成 AI 总结"}
                  </button>
                </EmptyPanel>
              )}
            </div>
          </section>
          <section
            role="tabpanel"
            id="panel-mindmap"
            aria-labelledby="tab-mindmap"
            hidden={tab !== "mindmap"}
            className="content-panel content-panel-scrollable"
          >
            <div className="content-panel-heading">
              <div>
                <h2>思维导图</h2>
                <p>
                  {transcript
                    ? "使用已有文稿，梳理内容的整体结构。"
                    : "一次整理，同时获得总结与思维导图。"}
                </p>
              </div>
              {mindmap && (
                <button
                  className="text-button"
                  disabled={contentActive || !!quoting}
                  onClick={() =>
                    void create("ai_mindmap", "获取总结与思维导图")
                  }
                >
                  重新获取
                </button>
              )}
            </div>
            <div
              className="content-panel-body"
              role="region"
              aria-label="思维导图内容"
              tabIndex={0}
            >
              {mindmap ? (
                <MindMapViewer result={mindmap} />
              ) : (
                <EmptyPanel
                  icon="map"
                  title={
                    busy("ai_mindmap") ? "正在整理知识结构" : "把要点连成一张图"
                  }
                  description={
                    busy("ai_mindmap")
                      ? "总结与导图生成后，可在这里展开、缩放和导出。"
                      : "将主题、结论与细节连接起来，帮助理解内容之间的关系。"
                  }
                >
                  <button
                    className="button primary"
                    disabled={!canAI || contentActive || !!quoting}
                    onClick={() =>
                      void create("ai_mindmap", "生成总结与思维导图")
                    }
                  >
                    {busy("ai_mindmap") ? "正在构建…" : "生成总结与导图"}
                  </button>
                </EmptyPanel>
              )}
            </div>
          </section>
          <section
            role="tabpanel"
            id="panel-questions"
            aria-labelledby="tab-questions"
            hidden={tab !== "questions"}
            className="content-panel content-panel-scrollable"
          >
            <div className="content-panel-heading">
              <div>
                <h2>内容问答</h2>
                <p>带着问题阅读，让回答回到原文。</p>
              </div>
            </div>
            <div
              className="content-panel-body"
              role="region"
              aria-label="问答内容"
              tabIndex={0}
            >
              {transcript ? (
                <VideoQuestions
                  video={video}
                  transcriptId={transcript.id}
                  onJump={setCitation}
                />
              ) : (
                <EmptyPanel
                  icon="chat"
                  title="获取文稿后，就可以提问"
                  description="先提取文稿或生成总结，再针对这份内容提问。"
                >
                  <button
                    className="button secondary"
                    onClick={() => setTab("transcript")}
                  >
                    前往文稿
                  </button>
                </EmptyPanel>
              )}
            </div>
          </section>
          {showClips && (
            <section
              role="tabpanel"
              id="panel-clips"
              aria-labelledby="tab-clips"
              hidden={tab !== "clips"}
              className="content-panel"
            >
              <ClipWorkspace
                key={`${user?.id || "guest"}:${video.key}`}
                video={video}
                transcriptId={transcript?.id}
                resource={clip}
                active={tab === "clips"}
                creationEnabled={clipsCreationEnabled()}
                onTask={(taskId) =>
                  setParams(
                    (previous) => {
                      const next = new URLSearchParams(previous);
                      next.set("clip_task", taskId);
                      next.delete("clip_plan");
                      next.delete("clip_export");
                      return next;
                    },
                    { replace: true },
                  )
                }
                onPlan={(plan) =>
                  setParams(
                    (previous) => {
                      const next = new URLSearchParams(previous);
                      next.set("clip_plan", plan.id);
                      next.delete("clip_task");
                      next.delete("clip_export");
                      return next;
                    },
                    { replace: true },
                  )
                }
              />
            </section>
          )}
        </section>
        {citation && transcript && (
          <CitationDrawer
            transcript={transcript}
            focus={citation}
            sourceUrl={video.url}
            onClose={() => setCitation(undefined)}
          />
        )}
      </div>
      {proposal && (
        <AllowanceConfirm
          quote={proposal.quote}
          label={proposal.label}
          sourceNote={
            proposal.text ? "使用已有文稿，无需重新转写。" : undefined
          }
          pending={confirming}
          error={quoteError}
          onConfirm={() => void confirm()}
          onClose={() => {
            if (!lock.current) setProposal(undefined);
          }}
          onRefresh={() =>
            void create(proposal.kind, proposal.label, proposal.presetId)
          }
        />
      )}
      {deleteOpen && (
        <dialog
          ref={deleteDialog}
          className="billing-dialog source-delete-dialog"
          aria-labelledby="delete-source-title"
          onCancel={(event) => {
            event.preventDefault();
            if (!deleting) setDeleteOpen(false);
          }}
        >
          <span className="billing-dialog-icon">
            <Icon name="trash" size={25} />
          </span>
          <h2 id="delete-source-title">删除原文件？</h2>
          <p>文稿、总结、导图、已获得的回答和收藏会继续保留。</p>
          {contentActive && (
            <p className="quote-warning">
              这份内容仍有任务正在处理。确认后将先取消处理，等待服务端确认后再删除原文件。
            </p>
          )}
          <ErrorNotice error={deleteError} />
          <div className="quote-actions">
            <button
              className="button secondary"
              disabled={deleting}
              onClick={() => setDeleteOpen(false)}
            >
              保留原文件
            </button>
            <button
              className="button primary"
              disabled={deleting}
              onClick={async () => {
                if (!video.source?.uploadId) return;
                setDeleting(true);
                setDeleteError(null);
                try {
                  await store.uploads.remove(video.source.uploadId, true);
                  if (alive.current) setDeleteOpen(false);
                } catch (cause) {
                  if (alive.current) setDeleteError(normalizeError(cause));
                } finally {
                  if (alive.current) setDeleting(false);
                }
              }}
            >
              {deleting
                ? "正在处理…"
                : contentActive
                  ? "取消处理并删除原文件"
                  : "删除原文件"}
            </button>
          </div>
        </dialog>
      )}
    </div>
  );
}
function EmptyPanel({
  icon,
  title,
  description,
  children,
}: {
  icon: string;
  title: string;
  description: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="content-empty">
      <span className="content-empty-icon">
        <Icon name={icon} size={29} />
      </span>
      <h3>{title}</h3>
      <p>{description}</p>
      {children && <div className="workspace-empty-actions">{children}</div>}
    </div>
  );
}
function LocalPreview({ id, audio }: { id: string; audio: boolean }) {
  const store = useWorkspace(),
    [url, setUrl] = useState<string>(),
    [failed, setFailed] = useState(false);
  useEffect(() => {
    setUrl(store.uploads.preview(id));
    setFailed(false);
  }, [store, id]);
  if (!url)
    return (
      <p className="local-preview-note">
        原文件暂不支持在线预览，文稿和已获得的结果可继续使用。
      </p>
    );
  return (
    <details className="local-media-preview">
      <summary>
        <Icon name={audio ? "audio" : "play"} size={15} />
        预览本地{audio ? "音频" : "视频"}
        <span>刷新页面后预览将不可用</span>
      </summary>
      {failed ? (
        <p>当前浏览器暂不支持预览此格式，可以继续提取文稿。</p>
      ) : audio ? (
        <audio
          src={url}
          controls
          preload="metadata"
          onError={() => setFailed(true)}
        />
      ) : (
        <video
          src={url}
          controls
          preload="metadata"
          onError={() => setFailed(true)}
        />
      )}
    </details>
  );
}
