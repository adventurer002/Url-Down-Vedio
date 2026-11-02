import type { HistoryVideo, JobKind } from "../types/api";
import type {
  ContentInput,
  UploadRecord,
  UploadStatus,
} from "../types/uploads";
import type { SavedVideo, LocalTask } from "../stores/workspace";

export interface UploadedSource {
  kind: "upload";
  uploadId?: string;
  mediaType: "audio" | "video";
  sizeBytes?: number;
  expiresAt?: string;
  status: UploadStatus | "unknown";
}
export function contentKey(video: HistoryVideo) {
  return video.source_kind === "upload" ||
    !video.platform ||
    !video.platform_video_id
    ? `content_${video.id}`
    : `${video.platform}_${video.platform_video_id}`;
}
export function fromHistory(video: HistoryVideo): SavedVideo {
  return {
    key: contentKey(video),
    contentId: video.id,
    url: video.source_url || "",
    partial: video.source_kind !== "upload",
    source:
      video.source_kind === "upload"
        ? {
            kind: "upload",
            mediaType: video.media_type || "video",
            status: "ready",
          }
        : undefined,
    metadata: {
      platform: video.platform || "",
      platform_video_id: video.platform_video_id || "",
      title: video.title,
      duration: video.duration,
      thumbnail: video.thumbnail,
      uploader: video.uploader,
      has_subtitles: !!video.subtitle_languages?.length,
      subtitle_languages: video.subtitle_languages || [],
      download_options: [],
    },
  };
}
export function fromUpload(upload: UploadRecord): SavedVideo {
  return {
    key: upload.video_id ? `content_${upload.video_id}` : `upload_${upload.id}`,
    contentId: upload.video_id || undefined,
    url: "",
    source: {
      kind: "upload",
      uploadId: upload.id,
      mediaType:
        upload.media_type ||
        (/\.(mp3|m4a)$/i.test(upload.filename) ? "audio" : "video"),
      sizeBytes: upload.size_bytes || upload.expected_bytes,
      expiresAt: upload.expires_at,
      status: upload.status,
    },
    metadata: {
      platform: "",
      platform_video_id: "",
      title: upload.filename,
      duration: upload.duration_seconds,
      thumbnail: null,
      uploader: null,
      has_subtitles: false,
      subtitle_languages: [],
      download_options: [],
    },
  };
}
export function sourceLabel(video: SavedVideo) {
  return video.source?.kind === "upload"
    ? video.source.mediaType === "audio"
      ? /\.m4a$/i.test(video.metadata.title)
        ? "M4A 音频"
        : /\.mp3$/i.test(video.metadata.title)
          ? "MP3 音频"
          : "本地音频"
      : "本地视频"
    : "视频链接";
}
export function sourceExpired(video: SavedVideo, now = Date.now()) {
  return (
    video.source?.kind === "upload" &&
    (["expired", "deleted"].includes(video.source.status) ||
      (!!video.source.expiresAt && Date.parse(video.source.expiresAt) <= now))
  );
}
export function processingInput(
  video: SavedVideo,
  kind: JobKind,
  transcriptId?: string,
): ContentInput {
  if (
    transcriptId &&
    ["ai_summary", "ai_mindmap", "ai_clip_plan"].includes(kind)
  )
    return { transcript_id: transcriptId };
  if (video.source?.kind === "upload") {
    if (!video.source.uploadId || sourceExpired(video))
      throw new Error("原文件已到期，请使用已有文稿继续整理，或重新上传文件。");
    return { upload_id: video.source.uploadId };
  }
  return { url: video.url };
}
export function taskLocation(task: LocalTask) {
  const value = task.snapshot?.result_payload;
  const tab =
    (
      {
        ai_summary: "summary",
        ai_mindmap: "mindmap",
        ai_question: "questions",
        ai_clip_plan: "clips",
        clips_export: "clips",
        transcription: "transcript",
        media_probe: "transcript",
      } as Record<string, string>
    )[task.kind] || "media";
  const params = new URLSearchParams({ tab });
  if (tab === "clips") {
    if (value?.clip_export_id) params.set("clip_export", value.clip_export_id);
    else if (value?.clip_plan_id) params.set("clip_plan", value.clip_plan_id);
    else params.set("clip_task", task.id);
  }
  if (tab !== "clips" && value?.transcript_id && !value.result_id)
    params.set("transcript", value.transcript_id);
  if (value?.result_id) params.set("result", value.result_id);
  if (value?.answer_id) params.set("answer", value.answer_id);
  return `/video/${encodeURIComponent(task.videoKey)}?${params}`;
}
