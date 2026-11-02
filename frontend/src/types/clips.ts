import type { ContentInput } from "./uploads";

export interface ClipStrategy {
  count: number;
  duration_mode?: "auto" | "manual";
  min_seconds: number | null;
  max_seconds: number | null;
  focus: string;
}
export type ClipPlanQuoteInput = ContentInput & {
  strategy: ClipStrategy;
  language: string;
};
export interface ClipCandidate {
  id: string;
  title: string;
  reason: string;
  topic: string;
  quote: string;
  start: number;
  end: number;
  timing_precision: "coarse" | "segment";
  needs_alignment: boolean;
}
export interface ClipSource {
  video_id: string;
  source_kind: "link" | "upload";
  media_type: "video" | "audio";
  title: string;
  duration: number | null;
  url: string | null;
  platform: string | null;
  platform_video_id: string | null;
}
export interface ClipPlan {
  id: string;
  task_id: string;
  video_id: string;
  transcript_id: string;
  revision: number;
  spec: { strategy: ClipStrategy; language: string };
  candidates: ClipCandidate[];
  outcome: "highlights" | "no_highlights";
  reason: string;
  source_basis: "subtitle" | "asr";
  source: ClipSource;
  source_available: boolean | null;
  source_status:
    | "available_upload"
    | "public_link_requires_fetch"
    | "source_required"
    | "audio_only";
  source_required: boolean;
  mock?: boolean;
  created_at: string;
}
export interface ClipExportQuoteInput {
  plan_id: string;
  revision: number;
  candidate_ids: string[];
  profile: "video_720_mp4";
}
export interface ClipExportItem {
  candidate_id: string;
  file_id: string;
  title: string;
  start: number;
  end: number;
  core_start?: number;
  core_end?: number;
  size_bytes?: number;
  filename?: string;
  verification?: { duration: number; decoded: boolean };
  expires_at: string;
  download_available: boolean;
}
export interface ClipExport {
  id: string;
  task_id: string;
  plan_id: string;
  revision: number;
  items: ClipExportItem[];
  created_at?: string;
}
