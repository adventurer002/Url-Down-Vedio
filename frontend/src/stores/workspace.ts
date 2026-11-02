import { createUploadManager } from "./uploads";
import { fromHistory, fromUpload, type UploadedSource } from "../lib/content";
import type { UploadRecord } from "../types/uploads";
import type { HistoryVideo, TaskCreated } from "../types/api";
import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import { billingStore } from "./billing";
import { createTaskTracker, terminal } from "../lib/taskTracker";
import type {
  JobKind,
  TaskEvent,
  TaskSnapshot,
  VideoParseResult,
} from "../types/api";
import type { ClipPlan, ClipExportQuoteInput } from "../types/clips";

export type PendingClipExport = ClipExportQuoteInput & { quoteId: string };

export interface SavedVideo {
  key: string;
  contentId?: string;
  clipPlanId?: string;
  pendingClipQuoteId?: string;
  pendingClipExport?: PendingClipExport;
  clipSelection?: ClipExportQuoteInput;
  clipExports?: { planId: string; exportId: string }[];
  aliases?: string[];
  source?: UploadedSource;
  url: string;
  metadata: VideoParseResult;
  partial?: boolean;
  results?: { transcriptId: string; summaryId?: string; mindmapId?: string };
}
export interface LocalTask {
  id: string;
  videoKey: string;
  title: string;
  kind: string;
  label: string;
  createdAt: string;
  event: TaskEvent;
  snapshot?: TaskSnapshot;
  trackingError?: string;
  videoAvailable?: boolean;
  historyHidden?: boolean;
  clipPlanId?: string;
}
function read<T>(key: string, fallback: T): T {
  try {
    return JSON.parse(localStorage.getItem(key) || "null") ?? fallback;
  } catch {
    return fallback;
  }
}
function write(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* Private/full storage must not prevent normal operation. */
  }
}
function readVideos(identity: string): SavedVideo[] {
  const saved = read<SavedVideo[]>("videohub.videos.v1" + identity, []);
  return Array.isArray(saved)
    ? saved
        .filter(
          (v) =>
            v?.key &&
            v?.metadata?.title &&
            (v.source?.kind === "upload" ||
              (v.metadata.platform_video_id && v.url)),
        )
        .slice(0, 30)
        .map((video) => ({
          ...video,
          clipSelection: validExportInput(video.clipSelection)
            ? video.clipSelection
            : undefined,
          pendingClipExport:
            validExportInput(video.pendingClipExport) &&
            typeof video.pendingClipExport?.quoteId === "string"
              ? video.pendingClipExport
              : undefined,
          clipExports: Array.isArray(video.clipExports)
            ? video.clipExports
                .filter(
                  (entry) =>
                    entry &&
                    typeof entry.planId === "string" &&
                    typeof entry.exportId === "string",
                )
                .slice(0, 10)
            : [],
        }))
    : [];
}
function validExportInput(value: unknown): value is ClipExportQuoteInput {
  const input = value as ClipExportQuoteInput | undefined;
  return (
    !!input &&
    typeof input.plan_id === "string" &&
    Number.isInteger(input.revision) &&
    input.revision >= 1 &&
    input.profile === "video_720_mp4" &&
    Array.isArray(input.candidate_ids) &&
    input.candidate_ids.length <= 5 &&
    input.candidate_ids.every((id) => typeof id === "string")
  );
}
function readTasks(identity: string) {
  const saved = read<LocalTask[]>("videohub.tasks.v1" + identity, []);
  return Array.isArray(saved)
    ? saved
        .filter((t) => t?.id && t?.event?.status && t?.videoKey)
        .filter((t, i) => i < 50 || !terminal(t.event.status))
    : [];
}
export function createWorkspace() {
  let version = 0;
  let trackingEnabled = true;
  let identity = "";
  let identityVersion = 0;
  let historyRevision = 0;
  const listeners = new Set<() => void>();
  function notify() {
    version++;
    listeners.forEach((listener) => listener());
  }
  const state = {
    videos: readVideos(identity),
    tasks: readTasks(identity),
    historyOpen: false,
    submitting: [] as string[],
    historyIds: [] as string[],
    historyTotal: 0,
    historyOffset: 0,
    historyLoading: false,
    historyMutating: false,
    historyLoaded: false,
  };
  const jobLabels: Record<string, string> = {
    download: "视频下载",
    audio_extraction: "纯音频提取",
    transcription: "字幕 / 语音转写",
    ai_summary: "AI 结构化总结",
    ai_mindmap: "AI 思维导图",
    ai_question: "内容问答",
    ai_clip_plan: "寻找精彩片段",
    clips_export: "生成视频片段",
    media_probe: "文件校验",
    simulate_progress: "系统演示任务",
  };
  async function loadHistory(reset = true) {
    if (state.historyLoading || state.historyMutating) return;
    const expectedIdentity = identityVersion;
    const expectedHistory = historyRevision;
    state.historyLoading = true;
    notify();
    try {
      const page = await api.history(20, reset ? 0 : state.historyOffset);
      if (
        expectedIdentity !== identityVersion ||
        expectedHistory !== historyRevision
      )
        return;
      const pageIds: string[] = [];
      for (const snapshot of page.items) {
        let v = snapshot.video;
        let restored: SavedVideo | undefined;
        if (
          snapshot.task_type === "media_probe" ||
          (v?.source_kind === "upload" &&
            !state.videos.some(
              (item) => item.contentId === v!.id && item.source?.uploadId,
            ))
        ) {
          try {
            const full = await api.task(snapshot.task_id);
            if (
              expectedIdentity !== identityVersion ||
              expectedHistory !== historyRevision
            )
              return;
            const uploadId =
              full.input_payload?.upload_id || full.result_payload?.upload_id;
            if (typeof uploadId === "string") {
              const record = await api.upload(uploadId);
              if (
                expectedIdentity !== identityVersion ||
                expectedHistory !== historyRevision
              )
                return;
              uploads.adopt(record);
              restored = state.videos.find(
                (item) => item.source?.uploadId === record.id,
              );
            }
          } catch {
            /* Saved transcript results remain readable even if the original file cannot be restored. */
          }
        }
        if (!restored && v) {
          const found = fromHistory(v);
          restored = state.videos.find(
            (item) =>
              item.key === found.key ||
              (!!item.contentId && item.contentId === v!.id),
          );
          if (!restored) {
            state.videos.push(found);
            restored = found;
          } else if (!restored.contentId) restored.contentId = v.id;
        }
        const videoKey = restored?.key || `system_${snapshot.task_id}`;
        const existing = state.tasks.find((t) => t.id === snapshot.task_id);
        const event =
          existing &&
          existing.event.seq > snapshot.seq &&
          !terminal(snapshot.status)
            ? existing.event
            : {
                ...snapshot,
                seq: Math.max(snapshot.seq, existing?.event.seq ?? -1),
              };
        const item: LocalTask = {
          id: snapshot.task_id,
          videoKey,
          videoAvailable: !!restored,
          historyHidden: false,
          title: restored?.metadata.title || snapshot.video_title || "系统任务",
          kind: snapshot.task_type,
          label: jobLabels[snapshot.task_type] || snapshot.task_type,
          createdAt: snapshot.created_at,
          event,
          snapshot,
        };
        if (existing) Object.assign(existing, item);
        else state.tasks.push(item);
        pageIds.push(item.id);
        if (!terminal(snapshot.status)) track(existing || item);
      }
      state.tasks.sort(
        (a, b) =>
          Date.parse(b.createdAt) - Date.parse(a.createdAt) ||
          b.id.localeCompare(a.id),
      );
      state.historyIds = [
        ...new Set(reset ? pageIds : [...state.historyIds, ...pageIds]),
      ];
      state.historyTotal = page.total;
      state.historyOffset = page.offset + page.items.length;
      state.historyLoaded = true;
      persist();
    } finally {
      if (expectedIdentity === identityVersion) {
        state.historyLoading = false;
        notify();
      }
    }
  }
  async function deleteHistory(taskId?: string) {
    if (state.historyMutating) return;
    const expectedIdentity = identityVersion;
    const affected = new Set(
      taskId ? [taskId] : state.tasks.map((task) => task.id),
    );
    const previousIds = new Set(state.historyIds);
    state.historyMutating = true;
    historyRevision++;
    notify();
    try {
      const result = taskId
        ? await api.deleteHistory(taskId)
        : await api.clearHistory();
      if (expectedIdentity !== identityVersion) return;
      for (const task of state.tasks)
        if (affected.has(task.id)) task.historyHidden = true;
      state.historyIds = state.historyIds.filter((id) => !affected.has(id));
      const removedLoaded = [...previousIds].filter((id) =>
        affected.has(id),
      ).length;
      state.historyTotal = Math.max(
        0,
        state.historyTotal - result.deleted_count,
      );
      state.historyOffset = taskId
        ? Math.max(0, state.historyOffset - removedLoaded)
        : 0;
      state.historyLoaded = true;
      persist();
    } finally {
      if (expectedIdentity === identityVersion) {
        historyRevision++;
        state.historyMutating = false;
        notify();
      }
    }
  }
  const trackers = new Map<string, ReturnType<typeof createTaskTracker>>();
  let persistTimer: ReturnType<typeof setTimeout> | undefined;
  function flushPersistence() {
    if (!persistTimer) return;
    clearTimeout(persistTimer);
    persistTimer = undefined;
    writeCache();
  }
  function writeCache() {
    write("videohub.videos.v1" + identity, state.videos);
    write("videohub.tasks.v1" + identity, state.tasks);
  }
  function persist(deferred = false) {
    if (deferred) {
      persistTimer ??= setTimeout(flushPersistence, 1000);
    } else {
      if (persistTimer) clearTimeout(persistTimer);
      persistTimer = undefined;
      writeCache();
    }
    notify();
  }
  function remember(
    url: string,
    metadata: VideoParseResult,
    partial = false,
    results?: SavedVideo["results"],
  ) {
    const key = `${metadata.platform}_${metadata.platform_video_id}`;
    const previous = state.videos.find((v) => v.key === key);
    state.videos = [
      {
        key,
        url,
        metadata:
          partial && previous && !previous.partial
            ? previous.metadata
            : metadata,
        partial: partial && previous ? !!previous.partial : partial,
        results: results || previous?.results,
        clipPlanId: previous?.clipPlanId,
        pendingClipQuoteId: previous?.pendingClipQuoteId,
        pendingClipExport: previous?.pendingClipExport,
        clipSelection: previous?.clipSelection,
        clipExports: previous?.clipExports,
      },
      ...state.videos.filter((v) => v.key !== key),
    ].slice(0, 30);
    persist();
    return key;
  }
  function track(item: LocalTask) {
    if (!trackingEnabled || trackers.has(item.id)) return;
    const expectedIdentity = identityVersion;
    const tracker = createTaskTracker(item.id, {
      onEvent(event) {
        if (expectedIdentity !== identityVersion) return;
        const target = state.tasks.find((t) => t.id === item.id);
        if (target) {
          const changed =
            target.event.status !== event.status ||
            target.event.stage !== event.stage;
          target.event = event;
          target.trackingError = undefined;
          persist(!changed && !terminal(event.status));
        }
      },
      onSnapshot(snapshot) {
        if (expectedIdentity !== identityVersion) return;
        const target = state.tasks.find((t) => t.id === item.id);
        if (target) {
          target.snapshot = snapshot;
          if (snapshot.seq === 0 && terminal(snapshot.status))
            target.event = { ...snapshot, seq: target.event.seq };
          target.trackingError = undefined;
          persist(!terminal(snapshot.status));
          if (target.kind === "media_probe" && terminal(snapshot.status)) {
            const source = state.videos.find(
              (video) => video.key === target.videoKey,
            )?.source;
            const uploadId =
              snapshot.result_payload?.upload_id || source?.uploadId;
            if (uploadId) void uploads.refresh(uploadId);
          }
          if (terminal(snapshot.status)) void billingStore.refresh();
        }
      },
      onError(error) {
        if (expectedIdentity !== identityVersion) return;
        const target = state.tasks.find((t) => t.id === item.id);
        if (target) {
          target.trackingError = normalizeError(error).message;
          notify();
        }
      },
      onMode(mode) {
        if (mode === "stopped") trackers.delete(item.id);
      },
    });
    trackers.set(item.id, tracker);
  }
  async function create(
    video: SavedVideo,
    kind: JobKind,
    label: string,
    presetId?: string,
    quoteId?: string,
    recovering = false,
    exportInput?: ClipExportQuoteInput,
  ) {
    const key = `${video.key}:${kind}`;
    if (
      state.submitting.includes(key) ||
      (!recovering &&
        state.tasks.some(
          (t) =>
            t.videoKey === video.key &&
            t.kind === kind &&
            !terminal(t.event.status),
        ))
    )
      throw new ApiError(
        "TASK_ALREADY_RUNNING",
        "同类任务正在执行，请等待完成或取消。",
      );
    if (kind === "clips_export" && (!quoteId || !exportInput))
      throw new ApiError("QUOTE_REQUIRED", "请先选择片段并确认生成报价。");
    state.submitting.push(key);
    const expectedIdentity = identityVersion;
    const clipQuote = kind === "ai_clip_plan" ? quoteId : undefined;
    const exportQuote = kind === "clips_export" ? quoteId : undefined;
    if (clipQuote) setClipConfirmation(video.key, clipQuote);
    else if (exportQuote && exportInput)
      setClipExportConfirmation(video.key, {
        ...exportInput,
        candidate_ids: [...exportInput.candidate_ids],
        quoteId: exportQuote,
      });
    else notify();
    try {
      const created = quoteId
        ? await api.confirmQuote(quoteId)
        : await api.create(kind, video.url, presetId);
      if (expectedIdentity !== identityVersion)
        throw new ApiError("AUTH_CHANGED", "登录用户已变更，请刷新任务历史。");
      const task = registerTask(created, video, kind, label);
      if (exportQuote && exportInput) {
        task.clipPlanId = exportInput.plan_id;
        setClipExportConfirmation(video.key, undefined, exportQuote);
      }
      if (clipQuote) setClipConfirmation(video.key, undefined, clipQuote);
      return task;
    } catch (cause) {
      const problem = normalizeError(cause);
      const definite =
        (problem.status != null &&
          problem.status >= 400 &&
          problem.status < 500 &&
          problem.status !== 408) ||
        ["QUOTE_MISMATCH", "QUOTE_REQUIRED"].includes(problem.code);
      if (expectedIdentity === identityVersion && clipQuote && definite)
        setClipConfirmation(video.key, undefined, clipQuote);
      if (expectedIdentity === identityVersion && exportQuote && definite)
        setClipExportConfirmation(video.key, undefined, exportQuote);
      throw cause;
    } finally {
      if (expectedIdentity === identityVersion) {
        state.submitting = state.submitting.filter((v) => v !== key);
        notify();
      }
    }
  }
  async function cancel(item: LocalTask) {
    const expectedIdentity = identityVersion;
    const snapshot = await api.cancel(item.id);
    if (expectedIdentity !== identityVersion) return;
    item = state.tasks.find((task) => task.id === item.id) || item;
    // Cancellation response may race a newer SSE frame; never regress the event version.
    if (snapshot.seq >= item.event.seq || snapshot.seq === 0) {
      item.snapshot = snapshot;
      if (snapshot.seq >= item.event.seq) item.event = snapshot;
      else if (snapshot.seq === 0 && terminal(snapshot.status))
        item.event = { ...snapshot, seq: item.event.seq };
      persist();
      void billingStore.refresh();
    }
  }
  function registerTask(
    created: TaskCreated,
    video: SavedVideo,
    kind: string,
    label: string,
  ) {
    const previous = state.tasks.find((task) => task.id === created.task_id);
    if (previous) {
      previous.videoKey = video.key;
      previous.title = video.metadata.title;
      track(previous);
      return previous;
    }
    const item: LocalTask = {
      id: created.task_id,
      videoKey: video.key,
      title: video.metadata.title,
      kind,
      label,
      createdAt: new Date().toISOString(),
      event: {
        task_id: created.task_id,
        status: created.status,
        progress: 0,
        seq: -1,
        stage: "queued",
        error_code: null,
        updated_at: new Date().toISOString(),
      },
    };
    state.tasks = [item, ...state.tasks].filter(
      (task, i) => i < 50 || !terminal(task.event.status),
    );
    state.historyIds = [
      item.id,
      ...state.historyIds.filter((id) => id !== item.id),
    ];
    persist();
    track(item);
    return item;
  }
  function rememberUpload(record: UploadRecord) {
    const next = fromUpload(record);
    const matches = state.videos.filter(
      (video) =>
        video.key === next.key ||
        video.source?.uploadId === record.id ||
        (!!record.video_id && video.contentId === record.video_id),
    );
    const aliases = new Set(
      matches.flatMap((video) => [video.key, ...(video.aliases || [])]),
    );
    aliases.add(`upload_${record.id}`);
    aliases.delete(next.key);
    const oldKeys = matches.map((video) => video.key);
    next.aliases = [...aliases];
    next.results = matches.find((video) => video.results)?.results;
    next.clipPlanId = matches.find((video) => video.clipPlanId)?.clipPlanId;
    next.pendingClipQuoteId = matches.find(
      (video) => video.pendingClipQuoteId,
    )?.pendingClipQuoteId;
    next.pendingClipExport = matches.find(
      (video) => video.pendingClipExport,
    )?.pendingClipExport;
    next.clipSelection = matches.find(
      (video) => video.clipSelection,
    )?.clipSelection;
    next.clipExports = matches.find((video) => video.clipExports)?.clipExports;
    state.videos = [
      next,
      ...state.videos.filter((video) => !matches.includes(video)),
    ].slice(0, 30);
    for (const task of state.tasks)
      if (oldKeys.includes(task.videoKey)) task.videoKey = next.key;
    persist();
    return next.key;
  }
  function rememberContent(
    value: HistoryVideo,
    results?: SavedVideo["results"],
  ) {
    const next = fromHistory(value),
      previous = state.videos.find(
        (video) => video.key === next.key || video.contentId === value.id,
      );
    if (previous) {
      previous.results = results || previous.results;
      persist();
      return previous.key;
    }
    next.results = results;
    state.videos = [next, ...state.videos].slice(0, 30);
    persist();
    return next.key;
  }
  const uploads = createUploadManager({
    notify,
    record: rememberUpload,
    probe(record, created) {
      const video = state.videos.find(
        (item) => item.source?.uploadId === record.id,
      );
      if (video) registerTask(created, video, "media_probe", "文件校验");
    },
    tasks: () => state.tasks,
    cancelTask: cancel,
    snapshot(task, snapshot) {
      if (
        snapshot.seq >= task.event.seq ||
        (snapshot.seq === 0 && terminal(snapshot.status))
      ) {
        task.snapshot = snapshot;
        task.event = {
          ...snapshot,
          seq: Math.max(snapshot.seq, task.event.seq),
        };
        persist();
      }
    },
  });
  function rememberClipPlan(plan: ClipPlan) {
    const source = plan.source;
    const previous = state.videos.find(
      (video) =>
        video.contentId === plan.video_id ||
        (source.source_kind === "link" &&
          video.metadata.platform === source.platform &&
          video.metadata.platform_video_id === source.platform_video_id),
    );
    const key =
      previous?.key ||
      rememberContent({
        id: plan.video_id,
        source_kind: source.source_kind,
        media_type: source.media_type,
        platform: source.platform,
        platform_video_id: source.platform_video_id,
        source_url: source.url,
        title: source.title,
        duration: source.duration,
        thumbnail: null,
        uploader: null,
        subtitle_languages: [],
      });
    const video = state.videos.find((item) => item.key === key)!;
    video.contentId = plan.video_id;
    video.clipPlanId = plan.id;
    for (const task of state.tasks)
      if (task.snapshot?.video_id === plan.video_id) {
        task.videoKey = key;
        task.title = video.metadata.title;
        task.videoAvailable = true;
      }
    if (!previous && video.source) video.source.status = "unknown";
    persist();
    return key;
  }
  function adoptClipTask(snapshot: TaskSnapshot, videoKey: string) {
    if (!["ai_clip_plan", "clips_export"].includes(snapshot.task_type))
      throw new ApiError("CLIP_TASK_INVALID", "此链接不是切片任务");
    let item = state.tasks.find((task) => task.id === snapshot.task_id);
    if (!item) {
      item = {
        id: snapshot.task_id,
        videoKey,
        kind: snapshot.task_type,
        title: String(snapshot.input_payload.title || "切片内容"),
        label: jobLabels[snapshot.task_type],
        createdAt: snapshot.created_at,
        event: snapshot,
        snapshot,
      };
      state.tasks.unshift(item);
    } else if (snapshot.seq >= item.event.seq || terminal(snapshot.status)) {
      item.snapshot = snapshot;
      item.event = { ...snapshot, seq: Math.max(snapshot.seq, item.event.seq) };
    }
    if (!terminal(item.event.status)) track(item);
    persist();
    return item;
  }
  function setClipConfirmation(
    videoKey: string,
    quoteId?: string,
    expectedQuote?: string,
  ) {
    const video = state.videos.find((item) => item.key === videoKey);
    if (!video || (expectedQuote && video.pendingClipQuoteId !== expectedQuote))
      return;
    video.pendingClipQuoteId = quoteId;
    persist();
  }
  function recoverClipConfirmation(video: SavedVideo) {
    if (!video.pendingClipQuoteId)
      throw new ApiError("QUOTE_REQUIRED", "没有待恢复的确认");
    return create(
      video,
      "ai_clip_plan",
      "寻找精彩片段",
      undefined,
      video.pendingClipQuoteId,
      true,
    );
  }
  function setClipExportConfirmation(
    videoKey: string,
    pending?: PendingClipExport,
    expectedQuote?: string,
  ) {
    const video = state.videos.find(
      (item) => item.key === videoKey || item.aliases?.includes(videoKey),
    );
    if (
      !video ||
      (expectedQuote && video.pendingClipExport?.quoteId !== expectedQuote)
    )
      return;
    video.pendingClipExport = pending;
    persist();
  }
  function confirmClipExport(
    video: SavedVideo,
    quoteId: string,
    input: ClipExportQuoteInput,
  ) {
    return create(
      video,
      "clips_export",
      "生成视频片段",
      undefined,
      quoteId,
      false,
      input,
    );
  }
  function recoverClipExport(video: SavedVideo) {
    const pending = video.pendingClipExport;
    if (!pending) throw new ApiError("QUOTE_REQUIRED", "没有待恢复的生成确认");
    return create(
      video,
      "clips_export",
      "生成视频片段",
      undefined,
      pending.quoteId,
      true,
      pending,
    );
  }
  function setClipSelection(videoKey: string, input: ClipExportQuoteInput) {
    const video = state.videos.find((item) => item.key === videoKey);
    if (!video) return;
    video.clipSelection = {
      ...input,
      candidate_ids: [...new Set(input.candidate_ids)].slice(0, 5),
    };
    persist();
  }
  function rememberClipExport(
    videoKey: string,
    planId: string,
    exportId: string,
  ) {
    const video = state.videos.find(
      (item) => item.key === videoKey || item.aliases?.includes(videoKey),
    );
    if (
      !video ||
      video.clipExports?.some((entry) => entry.exportId === exportId)
    )
      return;
    video.clipExports = [
      { planId, exportId },
      ...(video.clipExports || []),
    ].slice(0, 10);
    persist();
  }
  function restore() {
    trackingEnabled = true;
    uploads.restore();
    state.tasks
      .filter((t) => !terminal(t.event.status) || !t.snapshot)
      .forEach(track);
  }
  function stopAll() {
    trackingEnabled = false;
    trackers.forEach((t) => t.stop());
    trackers.clear();
    flushPersistence();
  }
  return {
    get userId() {
      return identity ? identity.slice(1) : null;
    },
    get videos() {
      return state.videos;
    },
    get tasks() {
      return state.tasks;
    },
    get submitting() {
      return state.submitting;
    },
    get historyOpen() {
      return state.historyOpen;
    },
    setHistoryOpen(open: boolean) {
      state.historyOpen = open;
      notify();
    },
    get historyTasks() {
      return state.historyLoaded
        ? state.historyIds
            .map((id) => state.tasks.find((task) => task.id === id))
            .filter((task): task is LocalTask => !!task)
        : state.tasks.filter((task) => !task.historyHidden);
    },
    get historyTotal() {
      return state.historyTotal;
    },
    get historyOffset() {
      return state.historyOffset;
    },
    get historyLoading() {
      return state.historyLoading;
    },
    get historyMutating() {
      return state.historyMutating;
    },
    deleteHistory,
    get historyLoaded() {
      return state.historyLoaded;
    },
    get activeCount() {
      return state.tasks.filter((task) => !terminal(task.event.status)).length;
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    getVersion: () => version,
    switchUser(userId: string | null) {
      const next = userId ? ":" + userId : "";
      if (next === identity) return;
      stopAll();
      identity = next;
      identityVersion++;
      historyRevision++;
      state.historyMutating = false;
      state.tasks = readTasks(identity);
      state.videos = readVideos(identity);
      state.historyOpen = false;
      state.historyIds = [];
      state.historyTotal = state.historyOffset = 0;
      state.historyLoaded = state.historyLoading = false;
      state.submitting = [];
      uploads.switchUser(userId || "");
      notify();
      restore();
    },
    loadHistory,
    uploads,
    findContent(key: string) {
      return state.videos.find(
        (video) => video.key === key || video.aliases?.includes(key),
      );
    },
    rememberContent,
    rememberClipPlan,
    adoptClipTask,
    setClipConfirmation,
    recoverClipConfirmation,
    confirmClipExport,
    recoverClipExport,
    setClipExportConfirmation,
    setClipSelection,
    rememberClipExport,
    rememberUpload,
    remember,
    create,
    cancel,
    restore,
    stopAll,
    flushPersistence,
  };
}
export type Workspace = ReturnType<typeof createWorkspace>;
