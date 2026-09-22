export interface FormatInfo {
  format_id: string;
  ext: string;
  resolution?: string | null;
  filesize?: number | null;
  vcodec?: string | null;
  acodec?: string | null;
}

export type VideoStatus = "parsing" | "ready" | "failed";

export interface VideoDetail {
  id: string;
  title: string;
  uploader?: string | null;
  thumbnail_url?: string | null;
  duration_seconds?: number | null;
  platform: string;
  webpage_url: string;
  formats: FormatInfo[];
  status: VideoStatus;
  error_message?: string | null;
}

export type TaskStatus =
  | "queued"
  | "downloading"
  | "processing"
  | "uploading"
  | "completed"
  | "failed"
  | "cancelled";

export interface TaskDetail {
  id: string;
  video_id: string;
  format_id: string;
  quality: string;
  status: TaskStatus;
  progress: number;
  error_code?: string | null;
  error_message?: string | null;
  media_file_id?: string | null;
}

export interface ApiError {
  code: string;
  message: string;
  details: Record<string, unknown>;
}

export interface UserOut {
  id: string;
  email: string;
  nickname: string;
  is_admin: boolean;
}

export interface MeResponse {
  user: UserOut;
  plan_code: string;
  plan_name: string;
  today_downloads_used: number;
  today_downloads_quota: number;
}
