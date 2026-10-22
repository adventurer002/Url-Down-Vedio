export interface ApiErrorBody {
  code: string;
  message: string;
  request_id: string;
}
export interface ApiSuccess<T> {
  data: T;
}
export interface ApiFailure {
  error: ApiErrorBody;
}
export interface DownloadOption {
  id: string;
  label: string;
  container: string;
  height: number | null;
  estimated_size_bytes: number | null;
}
export interface VideoParseResult {
  platform: string;
  platform_video_id: string;
  title: string;
  duration: number | null;
  thumbnail: string | null;
  uploader: string | null;
  has_subtitles: boolean;
  subtitle_languages: string[];
  download_options: DownloadOption[];
}
export type TaskStatus =
  | "queued"
  | "running"
  | "processing"
  | "completed"
  | "failed"
  | "cancelled"
  | "expired";
export interface TaskEvent {
  task_id: string;
  status: TaskStatus;
  progress: number;
  seq: number;
  stage: string;
  error_code: string | null;
  updated_at: string;
}
export interface TaskResultPayload {
  file_id?: string;
  download_url?: string;
  transcript_id?: string;
  source_type?: "subtitle" | "asr";
  content_hash?: string;
  cache_hit?: boolean;
  result_id?: string;
  result_type?: "summary" | "mindmap";
  reused?: boolean;
  [key: string]: unknown;
}
export interface TaskSnapshot extends TaskEvent {
  video_id: string | null;
  task_type: string;
  input_payload: Record<string, unknown>;
  result_payload: TaskResultPayload | null;
  error_message: string | null;
  cancel_requested: boolean;
  attempts: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  expires_at: string | null;
}
export interface TaskCreated {
  task_id: string;
  status: TaskStatus;
}
export interface TranscriptSegment {
  start: number;
  end: number;
  text: string;
}
export interface TranscriptResult {
  id: string;
  video_id: string;
  task_id: string | null;
  source_type: "subtitle" | "asr";
  language: string;
  content_version: string;
  asr_model: string;
  content_text: string;
  content_hash: string;
  segments: TranscriptSegment[];
  created_at: string;
}
export interface ChapterSummary {
  start_seconds: number;
  timestamp: string;
  title: string;
  summary: string;
}
export interface VideoSummarySchema {
  overview: string;
  key_points: string[];
  chapters: ChapterSummary[];
  source_basis: "subtitle" | "asr";
}
export interface MindMapNode {
  title: string;
  children: MindMapNode[];
}
interface AIResultBase {
  id: string;
  video_id: string;
  task_id: string | null;
  transcript_id: string;
  source_content_hash: string;
  llm_provider: string;
  llm_model: string;
  prompt_version: string;
  output_language: string;
  markdown_export: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  duration_ms: number | null;
  estimated_cost_usd: number | null;
  created_at: string;
}
export interface SummaryResult extends AIResultBase {
  result_type: "summary";
  content_json: VideoSummarySchema;
}
export interface MindMapResult extends AIResultBase {
  result_type: "mindmap";
  content_json: MindMapNode;
}
export type AIResult = SummaryResult | MindMapResult;
export interface ReadinessStatus {
  status: "ready";
  database: "ready";
  redis: "ready";
}
export interface LivenessStatus {
  status: "alive";
}
export type JobKind =
  | "download"
  | "audio_extraction"
  | "transcription"
  | "ai_summary"
  | "ai_mindmap";

export interface HistoryVideo {
  id: string;
  platform: string;
  platform_video_id: string;
  source_url: string;
  title: string;
  duration: number | null;
  thumbnail: string | null;
  uploader: string | null;
  subtitle_languages: string[];
}
export interface TaskHistoryItem extends TaskSnapshot {
  video: HistoryVideo | null;
  video_title: string | null;
  download_available: boolean;
}
export interface TaskHistoryPage {
  items: TaskHistoryItem[];
  total: number;
  limit: number;
  offset: number;
}
export interface CleanupResult {
  files: number;
  temporary_directories: number;
  orphan_files: number;
  freed_bytes: number;
  used_bytes: number;
}
