import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import { createTaskTracker, terminal } from "../composables/useTaskPoller";
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
export const useWorkspace = defineStore("workspace", () => {
  const saved = read<SavedVideo[]>("videohub.videos.v1", []);
  const videos = ref<SavedVideo[]>(
    Array.isArray(saved)
      ? saved
          .filter((v) => v?.key && v?.metadata?.platform_video_id && v?.url)
          .slice(0, 30)
      : [],
  );
  const stored = read<LocalTask[]>("videohub.tasks.v1", []);
  const tasks = ref<LocalTask[]>(
    Array.isArray(stored)
      ? stored
          .filter((t) => t?.id && t?.event?.status && t?.videoKey)
          .slice(0, 50)
      : [],
  );
  const historyOpen = ref(false);
  const submitting = ref<string[]>([]);
  const historyIds = ref<string[]>([]),
    historyTotal = ref(0),
    historyOffset = ref(0);
  const historyLoading = ref(false),
    historyLoaded = ref(false);
  const historyTasks = computed(() =>
    historyLoaded.value
      ? historyIds.value
          .map((id) => tasks.value.find((t) => t.id === id))
          .filter((t): t is LocalTask => !!t)
      : tasks.value,
  );
  const jobLabels: Record<string, string> = {
    download: "视频下载",
    audio_extraction: "纯音频提取",
    transcription: "字幕 / 语音转写",
    ai_summary: "AI 结构化总结",
    ai_mindmap: "AI 思维导图",
    simulate_progress: "系统演示任务",
  };
  async function loadHistory(reset = true) {
    if (historyLoading.value) return;
    historyLoading.value = true;
    try {
      const page = await api.history(20, reset ? 0 : historyOffset.value);
      const pageIds: string[] = [];
      for (const snapshot of page.items) {
        const v = snapshot.video;
        const videoKey = v
          ? `${v.platform}_${v.platform_video_id}`
          : `system_${snapshot.task_id}`;
        if (v && !videos.value.some((item) => item.key === videoKey)) {
          videos.value.push({
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
        const existing = tasks.value.find((t) => t.id === snapshot.task_id);
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
        else tasks.value.push(item);
        pageIds.push(item.id);
        if (!terminal(snapshot.status)) track(existing || item);
      }
      tasks.value.sort(
        (a, b) =>
          Date.parse(b.createdAt) - Date.parse(a.createdAt) ||
          b.id.localeCompare(a.id),
      );
      historyIds.value = [
        ...new Set(reset ? pageIds : [...historyIds.value, ...pageIds]),
      ];
      historyTotal.value = page.total;
      historyOffset.value = page.offset + page.items.length;
      historyLoaded.value = true;
      persist();
    } finally {
      historyLoading.value = false;
    }
  }
  const trackers = new Map<string, ReturnType<typeof createTaskTracker>>();
  const activeCount = computed(
    () => tasks.value.filter((t) => !terminal(t.event.status)).length,
  );
  function persist() {
    write("videohub.videos.v1", videos.value);
    write("videohub.tasks.v1", tasks.value);
  }
  function remember(url: string, metadata: VideoParseResult) {
    const key = `${metadata.platform}_${metadata.platform_video_id}`;
    videos.value = [
      { key, url, metadata },
      ...videos.value.filter((v) => v.key !== key),
    ].slice(0, 30);
    persist();
    return key;
  }
  function track(item: LocalTask) {
    if (trackers.has(item.id)) return;
    const tracker = createTaskTracker(item.id, {
      onEvent(event) {
        const target = tasks.value.find((t) => t.id === item.id);
        if (target) {
          target.event = event;
          target.trackingError = undefined;
          persist();
        }
      },
      onSnapshot(snapshot) {
        const target = tasks.value.find((t) => t.id === item.id);
        if (target) {
          target.snapshot = snapshot;
          if (snapshot.seq === 0 && terminal(snapshot.status))
            target.event = { ...snapshot, seq: target.event.seq };
          target.trackingError = undefined;
          persist();
        }
      },
      onError(error) {
        const target = tasks.value.find((t) => t.id === item.id);
        if (target) target.trackingError = normalizeError(error).message;
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
      submitting.value.includes(key) ||
      tasks.value.some(
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
    submitting.value.push(key);
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
      tasks.value = [item, ...tasks.value].filter(
        (t, i) => i < 50 || !terminal(t.event.status),
      );
      historyIds.value = [
        item.id,
        ...historyIds.value.filter((id) => id !== item.id),
      ];
      persist();
      track(item);
      return item;
    } finally {
      submitting.value = submitting.value.filter((v) => v !== key);
    }
  }
  async function cancel(item: LocalTask) {
    const snapshot = await api.cancel(item.id);
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
    tasks.value
      .filter((t) => !terminal(t.event.status) || !t.snapshot)
      .forEach(track);
  }
  function stopAll() {
    trackers.forEach((t) => t.stop());
    trackers.clear();
  }
  return {
    videos,
    tasks,
    historyOpen,
    historyTasks,
    historyTotal,
    historyOffset,
    historyLoading,
    historyLoaded,
    loadHistory,
    submitting,
    activeCount,
    remember,
    create,
    cancel,
    restore,
    stopAll,
  };
});
