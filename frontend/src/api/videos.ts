import { apiFetch } from "./client";
import type { MeResponse, TaskDetail, UserOut, VideoDetail } from "../types";

export async function parseVideo(url: string): Promise<{ video_id: string }> {
  return apiFetch("/api/v1/videos/parse", {
    method: "POST",
    body: JSON.stringify({ url }),
  });
}

export async function getVideo(id: string): Promise<VideoDetail> {
  return apiFetch(`/api/v1/videos/${id}`);
}

export async function getVideos(page = 1, pageSize = 20) {
  return apiFetch<{ items: VideoDetail[]; total: number }>(
    `/api/v1/videos?page=${page}&page_size=${pageSize}`,
  );
}

export async function createDownload(
  videoId: string,
  formatId: string,
): Promise<{ task_id: string }> {
  return apiFetch("/api/v1/downloads", {
    method: "POST",
    body: JSON.stringify({ video_id: videoId, format_id: formatId }),
  });
}

export async function getTask(id: string): Promise<TaskDetail> {
  return apiFetch(`/api/v1/downloads/${id}`);
}

export async function cancelTask(id: string): Promise<TaskDetail> {
  return apiFetch(`/api/v1/downloads/${id}/cancel`, { method: "POST" });
}

export function downloadFileUrl(id: string): string {
  return `/api/v1/downloads/${id}/file`;
}

export async function register(
  email: string,
  password: string,
  nickname?: string,
): Promise<{ access_token: string; refresh_token: string; user: UserOut }> {
  return apiFetch("/api/v1/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password, nickname }),
  });
}

export async function login(
  email: string,
  password: string,
): Promise<{ access_token: string; refresh_token: string; user: UserOut }> {
  return apiFetch("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export async function fetchMe(): Promise<MeResponse> {
  return apiFetch("/api/v1/users/me");
}

const AI_PATH: Record<string, string> = {
  audio: "audio",
  transcribe: "transcribe",
  summarize: "summarize",
  mindmap: "mindmap",
};

export async function triggerAi(
  videoId: string,
  kind: "audio" | "transcribe" | "summarize" | "mindmap",
): Promise<{ task_id?: string; output_id?: string; reused: boolean }> {
  return apiFetch(`/api/v1/videos/${videoId}/${AI_PATH[kind]}`, { method: "POST" });
}

export async function getAiTask(id: string) {
  return apiFetch<{
    id: string;
    task_type: string;
    status: string;
    output_id?: string | null;
    error_code?: string | null;
    error_message?: string | null;
  }>(`/api/v1/tasks/${id}`);
}

export async function getTranscript(videoId: string) {
  return apiFetch<{
    id: string;
    language: string;
    text: string;
    segments: { start: number; end: number; text: string }[];
  }>(`/api/v1/videos/${videoId}/transcript`);
}

export async function getSummary(videoId: string) {
  return apiFetch<{
    id: string;
    summary: string;
    key_points: string[];
    chapters: { title: string; start: number; end: number }[];
    keywords: string[];
  }>(`/api/v1/videos/${videoId}/summary`);
}

export async function getMindmap(videoId: string) {
  return apiFetch<{ id: string; markdown: string }>(
    `/api/v1/videos/${videoId}/mindmap`,
  );
}
