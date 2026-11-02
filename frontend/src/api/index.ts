import type {
  UploadRecord,
  ContentInput,
  RequestIdentity,
} from "../types/uploads";
import { client, get, post, del } from "./client";
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
import type {
  ClipPlan,
  ClipPlanQuoteInput,
  ClipExport,
  ClipExportQuoteInput,
} from "../types/clips";
export const api = {
  clipPlanQuote: (input: ClipPlanQuoteInput, signal?: AbortSignal) =>
    post<AllowanceQuote>("/clips/plans/quotes", input, signal),
  clipPlan: (id: string, signal?: AbortSignal) =>
    get<ClipPlan>(`/clips/plans/${encodeURIComponent(id)}`, signal),
  clipExport: (id: string, signal?: AbortSignal) =>
    get<ClipExport>(`/clips/exports/${encodeURIComponent(id)}`, signal),
  clipExportQuote: (input: ClipExportQuoteInput, signal?: AbortSignal) =>
    post<AllowanceQuote>("/clips/exports/quotes", input, signal),
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
  createUpload: async (
    filename: string,
    sizeBytes: number,
    signal: AbortSignal,
    contentIdentity: RequestIdentity,
  ) =>
    (
      await client.post<UploadRecord>(
        "/uploads",
        { filename, size_bytes: sizeBytes },
        { signal, contentIdentity, timeout: 10000 },
      )
    ).data,
  uploadContent: async (
    id: string,
    file: File,
    signal: AbortSignal,
    onProgress: (loaded: number) => void,
    contentIdentity: RequestIdentity,
  ) =>
    (
      await client.put<UploadRecord>(
        `/uploads/${encodeURIComponent(id)}/content`,
        file,
        {
          signal,
          contentIdentity,
          timeout: 610000,
          headers: { "Content-Type": "application/octet-stream" },
          onUploadProgress: (event) => onProgress(event.loaded),
        },
      )
    ).data,
  completeUpload: async (
    id: string,
    signal?: AbortSignal,
    contentIdentity?: RequestIdentity,
  ) =>
    (
      await client.post<TaskCreated>(
        `/uploads/${encodeURIComponent(id)}/complete`,
        undefined,
        { signal, contentIdentity, timeout: 10000 },
      )
    ).data,
  upload: (id: string, signal?: AbortSignal) =>
    get<UploadRecord>(`/uploads/${encodeURIComponent(id)}`, signal),
  deleteUpload: (id: string) =>
    del<UploadRecord>(`/uploads/${encodeURIComponent(id)}`),
  ledger: (signal?: AbortSignal) =>
    get<{
      items: Array<{
        id: string;
        event: string;
        detail: {
          task_id?: string;
          asr_seconds?: number;
          ai_generations?: number;
        };
        created_at: string;
      }>;
    }>("/billing/ledger", signal),
  quote: (
    kind: JobKind,
    url: string | ContentInput,
    presetId?: string,
    signal?: AbortSignal,
  ) => {
    if (kind === "ai_clip_plan" || kind === "clips_export")
      throw new Error("切片任务需先获取专属报价");
    return post<AllowanceQuote>(
      "/billing/quotes",
      {
        action: kind,
        ...(typeof url === "string" ? { url } : url),
        ...(presetId ? { preset_id: presetId } : {}),
        language: "zh",
      },
      signal,
    );
  },
  confirmQuote: (quoteId: string) =>
    post<TaskCreated>("/billing/tasks", { quote_id: quoteId }),
  resumeQueued: () => post<{ dispatched: number }>("/billing/resume"),
  deleteHistory: (id: string) =>
    del<{ deleted_count: number }>(`/tasks/history/${encodeURIComponent(id)}`),
  clearHistory: () => del<{ deleted_count: number }>("/tasks/history"),
  history: (limit = 20, offset = 0, signal?: AbortSignal) =>
    get<TaskHistoryPage>(`/tasks?limit=${limit}&offset=${offset}`, signal),
  cleanup: () => post<CleanupResult>("/system/cleanup"),
  parse: (url: string, signal?: AbortSignal) =>
    post<VideoParseResult>("/videos/parse", { url }, signal),
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
    if (
      kind === "ai_question" ||
      kind === "ai_clip_plan" ||
      kind === "clips_export"
    )
      throw new Error("此任务需先确认专属报价");
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
