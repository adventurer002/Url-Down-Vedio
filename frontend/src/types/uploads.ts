export type UploadStatus =
  | "pending"
  | "uploaded"
  | "validating"
  | "ready"
  | "failed"
  | "expired"
  | "deleted";
export interface UploadRecord {
  id: string;
  filename: string;
  expected_bytes: number;
  size_bytes: number;
  content_sha256: string | null;
  status: UploadStatus;
  video_id: string | null;
  task_id: string | null;
  duration_seconds: number | null;
  media_type: "audio" | "video" | null;
  error_code: string | null;
  created_at: string;
  expires_at: string;
}
export interface RequestIdentity {
  userId: string;
  token: string;
}
export type ContentInput =
  | { url: string; upload_id?: never; transcript_id?: never }
  | { upload_id: string; url?: never; transcript_id?: never }
  | { transcript_id: string; url?: never; upload_id?: never };
export interface UploadTransfer {
  id: string;
  key: string;
  record: UploadRecord;
  phase:
    | "sending"
    | "confirming"
    | "validating"
    | "ready"
    | "interrupted"
    | "cancelling"
    | "cancelled"
    | "failed";
  loaded: number;
  error?: string;
  errorCode?: string;
}
