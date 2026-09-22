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
