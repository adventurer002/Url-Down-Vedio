import { get, post, del } from "./client";
import type {
  AIResult,
  TaskHistoryPage,
  CleanupResult,
  JobKind,
  ReadinessStatus,
  TaskCreated,
  TaskSnapshot,
  TranscriptResult,
  VideoParseResult,
} from "../types/api";
export const api = {
  deleteHistory: (id: string) =>
    del<{ deleted_count: number }>(`/tasks/history/${encodeURIComponent(id)}`),
  clearHistory: () => del<{ deleted_count: number }>("/tasks/history"),
  history: (limit = 20, offset = 0, signal?: AbortSignal) =>
    get<TaskHistoryPage>(`/tasks?limit=${limit}&offset=${offset}`, signal),
  cleanup: () => post<CleanupResult>("/system/cleanup"),
  parse: (url: string) => post<VideoParseResult>("/videos/parse", { url }),
  task: (id: string, signal?: AbortSignal) =>
    get<TaskSnapshot>(`/tasks/${encodeURIComponent(id)}`, signal),
  cancel: (id: string) =>
    post<TaskSnapshot>(`/tasks/${encodeURIComponent(id)}/cancel`),
  transcript: (id: string, signal?: AbortSignal) =>
    get<TranscriptResult>(`/transcriptions/${encodeURIComponent(id)}`, signal),
  result: (id: string, signal?: AbortSignal) =>
    get<AIResult>(`/ai/results/${encodeURIComponent(id)}`, signal),
  ready: (signal?: AbortSignal) =>
    get<ReadinessStatus>("/health/ready", signal),
  create: (kind: JobKind, url: string, presetId?: string) => {
    const path = {
      download: "/downloads",
      audio_extraction: "/audio/extractions",
      transcription: "/transcriptions",
      ai_summary: "/ai/summaries",
      ai_mindmap: "/ai/mindmaps",
    }[kind];
    const body =
      kind === "download"
        ? { url, preset_id: presetId }
        : kind === "audio_extraction"
          ? { url, format: "mp3" }
          : kind === "transcription"
            ? { url, language: null }
            : { url, language: "zh" };
    return post<TaskCreated>(path, body);
  },
};
