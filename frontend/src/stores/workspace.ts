import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import { createTaskTracker, terminal } from "../lib/taskTracker";
import type {
  JobKind,
  TaskEvent,
  TaskSnapshot,
  VideoParseResult,
} from "../types/api";
export interface SavedVideo {
  key: string;
  url: string;
  metadata: VideoParseResult;
  partial?: boolean;
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
export function createWorkspace() {
  let version = 0;
  let trackingEnabled = true;
  const listeners = new Set<() => void>();
  function notify() {
    version++;
    listeners.forEach((listener) => listener());
  }
  const saved = read<SavedVideo[]>("videohub.videos.v1", []);
  const stored = read<LocalTask[]>("videohub.tasks.v1", []);
  const state = {
    videos: Array.isArray(saved)
      ? saved
          .filter((v) => v?.key && v?.metadata?.platform_video_id && v?.url)
          .slice(0, 30)
      : [],
    tasks: Array.isArray(stored)
      ? stored
          .filter((t) => t?.id && t?.event?.status && t?.videoKey)
          .slice(0, 50)
      : [],
    historyOpen: false,
    submitting: [] as string[],
    historyIds: [] as string[],
    historyTotal: 0,
    historyOffset: 0,
    historyLoading: false,
    historyLoaded: false,
  };
  const jobLabels: Record<string, string> = {
    download: "视频下载",
    audio_extraction: "纯音频提取",
    transcription: "字幕 / 语音转写",
    ai_summary: "AI 结构化总结",
    ai_mindmap: "AI 思维导图",
    simulate_progress: "系统演示任务",
  };
  async function loadHistory(reset = true) {
    if (state.historyLoading) return;
    state.historyLoading = true;
    notify();
    try {
      const page = await api.history(20, reset ? 0 : state.historyOffset);
      const pageIds: string[] = [];
      for (const snapshot of page.items) {
        const v = snapshot.video;
        const videoKey = v
          ? `${v.platform}_${v.platform_video_id}`
          : `system_${snapshot.task_id}`;
        if (v && !state.videos.some((item) => item.key === videoKey)) {
          state.videos.push({
            key: videoKey,
            url: v.source_url,
            partial: true,
            metadata: {
              platform: v.platform,
              platform_video_id: v.platform_video_id,
              title: v.title,
              duration: v.duration,
              thumbnail: v.thumbnail,
              uploader: v.uploader,
              has_subtitles: !!v.subtitle_languages.length,
              subtitle_languages: v.subtitle_languages,
              download_options: [],
            },
          });
        }
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
          videoAvailable: !!v,
          title: snapshot.video_title || "系统任务",
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
      state.historyLoading = false;
      notify();
    }
  }
  const trackers = new Map<string, ReturnType<typeof createTaskTracker>>();
  function persist() {
    write("videohub.videos.v1", state.videos);
    write("videohub.tasks.v1", state.tasks);
    notify();
  }
  function remember(url: string, metadata: VideoParseResult) {
    const key = `${metadata.platform}_${metadata.platform_video_id}`;
    state.videos = [
      { key, url, metadata },
      ...state.videos.filter((v) => v.key !== key),
    ].slice(0, 30);
    persist();
    return key;
  }
  function track(item: LocalTask) {
    if (!trackingEnabled || trackers.has(item.id)) return;
    const tracker = createTaskTracker(item.id, {
      onEvent(event) {
        const target = state.tasks.find((t) => t.id === item.id);
        if (target) {
          target.event = event;
          target.trackingError = undefined;
          persist();
        }
      },
      onSnapshot(snapshot) {
        const target = state.tasks.find((t) => t.id === item.id);
        if (target) {
          target.snapshot = snapshot;
          if (snapshot.seq === 0 && terminal(snapshot.status))
            target.event = { ...snapshot, seq: target.event.seq };
          target.trackingError = undefined;
          persist();
        }
      },
      onError(error) {
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
  ) {
    const key = `${video.key}:${kind}`;
    if (
      state.submitting.includes(key) ||
      state.tasks.some(
        (t) =>
          t.videoKey === video.key &&
          t.kind === kind &&
          !terminal(t.event.status),
      )
    )
      throw new ApiError(
        "TASK_ALREADY_RUNNING",
        "同类任务正在执行，请等待完成或取消。",
      );
    state.submitting.push(key);
    notify();
    try {
      const created = await api.create(kind, video.url, presetId);
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
      // Keep active jobs, even when the history cap is reached.
      state.tasks = [item, ...state.tasks].filter(
        (t, i) => i < 50 || !terminal(t.event.status),
      );
      state.historyIds = [
        item.id,
        ...state.historyIds.filter((id) => id !== item.id),
      ];
      persist();
      track(item);
      return item;
    } finally {
      state.submitting = state.submitting.filter((v) => v !== key);
      notify();
    }
  }
  async function cancel(item: LocalTask) {
    const snapshot = await api.cancel(item.id);
    item = state.tasks.find((task) => task.id === item.id) || item;
    // Cancellation response may race a newer SSE frame; never regress the event version.
    if (snapshot.seq >= item.event.seq || snapshot.seq === 0) {
      item.snapshot = snapshot;
      if (snapshot.seq >= item.event.seq) item.event = snapshot;
      else if (snapshot.seq === 0 && terminal(snapshot.status))
        item.event = { ...snapshot, seq: item.event.seq };
      persist();
    }
  }
  function restore() {
    trackingEnabled = true;
    state.tasks
      .filter((t) => !terminal(t.event.status) || !t.snapshot)
      .forEach(track);
  }
  function stopAll() {
    trackingEnabled = false;
    trackers.forEach((t) => t.stop());
    trackers.clear();
  }
  return {
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
        : state.tasks;
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
    loadHistory,
    remember,
    create,
    cancel,
    restore,
    stopAll,
  };
}
export type Workspace = ReturnType<typeof createWorkspace>;
