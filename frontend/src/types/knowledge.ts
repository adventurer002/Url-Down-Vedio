import type { HistoryVideo } from "./api";
export interface Citation {
  id: number;
  start: number;
  end: number;
  quote: string;
  approximate: boolean;
}
export interface VideoAnswer {
  id: string;
  task_id: string;
  transcript_id: string;
  question: string;
  answer: string;
  citations: Citation[];
  model: string;
  prompt_version: string;
  created_at: string;
}
export interface LibraryItem {
  id: string;
  transcript_id: string;
  result_id: string | null;
  title: string;
  video: HistoryVideo;
  source_type: "subtitle" | "asr";
  tags: string[];
  excerpt: string;
  created_at: string;
  updated_at: string;
}
export interface LibraryDetail extends LibraryItem {
  content_text: string;
  summary_text: string;
}
export interface LibraryPage {
  items: LibraryItem[];
  total: number;
  limit: number;
  offset: number;
}
