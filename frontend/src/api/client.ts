import type { ApiError } from "../types";

export class ApiRequestError extends Error {
  code: string;
  status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

function baseUrl(): string {
  return "";
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem("access_token");
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init.headers as Record<string, string> | undefined),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${baseUrl()}${path}`, { ...init, headers });
  if (res.status === 204) return undefined as T;

  const isJson = res.headers.get("content-type")?.includes("json");
  const body = isJson ? await res.json() : null;

  if (!res.ok) {
    const err = (body?.error ?? {}) as Partial<ApiError>;
    throw new ApiRequestError(
      err.code ?? "internal_error",
      err.message ?? `请求失败 (${res.status})`,
      res.status,
    );
  }
  return body as T;
}

export function formatDuration(totalSeconds?: number | null): string {
  if (totalSeconds == null) return "--:--";
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function formatBytes(bytes?: number | null): string {
  if (bytes == null) return "--";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
