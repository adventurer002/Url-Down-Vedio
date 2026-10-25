import { get, post, del } from "./client";
import type {
  Allowances,
  AllowanceQuote,
  BillingCatalog,
} from "../types/billing";
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
import type {
  LibraryPage,
  LibraryDetail,
  VideoAnswer,
} from "../types/knowledge";
export const api = {
  questionQuote: (
    transcriptId: string,
    question: string,
    signal?: AbortSignal,
  ) =>
    post<AllowanceQuote>(
      "/knowledge/questions/quotes",
      { transcript_id: transcriptId, question, language: "zh" },
      signal,
    ),
  answer: (id: string, signal?: AbortSignal) =>
    get<VideoAnswer>(`/knowledge/answers/${encodeURIComponent(id)}`, signal),
  answers: (transcriptId: string, signal?: AbortSignal) =>
    get<{ items: VideoAnswer[] }>(
      `/knowledge/answers?transcript_id=${encodeURIComponent(transcriptId)}`,
      signal,
    ),
  library: (
    query = "",
    tag = "",
    offset = 0,
    signal?: AbortSignal,
    transcriptId = "",
  ) =>
    get<LibraryPage>(
      `/knowledge/library?${new URLSearchParams({ q: query, tag, offset: String(offset), ...(transcriptId ? { transcript_id: transcriptId } : {}) })}`,
      signal,
    ),
  libraryItem: (id: string, signal?: AbortSignal) =>
    get<LibraryDetail>(`/knowledge/library/${encodeURIComponent(id)}`, signal),
  saveVideo: (
    transcriptId: string,
    resultId: string | undefined,
    tags: string[],
  ) =>
    post<LibraryDetail>("/knowledge/library", {
      transcript_id: transcriptId,
      ...(resultId ? { result_id: resultId } : {}),
      tags,
    }),
  unsaveVideo: (id: string) =>
    del<{ removed: boolean }>(`/knowledge/library/${encodeURIComponent(id)}`),
  catalog: (signal?: AbortSignal) =>
    get<BillingCatalog>("/billing/catalog", signal),
  allowances: (signal?: AbortSignal) =>
    get<Allowances>("/billing/allowances", signal),
  quote: (
    kind: JobKind,
    url: string,
    presetId?: string,
    signal?: AbortSignal,
  ) =>
    post<AllowanceQuote>(
      "/billing/quotes",
      {
        action: kind,
        url,
        ...(presetId ? { preset_id: presetId } : {}),
        language: "zh",
      },
      signal,
    ),
  confirmQuote: (quoteId: string) =>
    post<TaskCreated>("/billing/tasks", { quote_id: quoteId }),
  resumeQueued: () => post<{ dispatched: number }>("/billing/resume"),
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
    if (kind === "ai_question") throw new Error("视频问答需先确认专属报价");
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
