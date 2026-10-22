import axios from "axios";
import type { ApiErrorBody } from "../types/api";
export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public request_id = "",
    public status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
export function normalizeError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  if (axios.isAxiosError(error)) {
    const body = error.response?.data?.error as ApiErrorBody | undefined;
    if (
      body &&
      typeof body.code === "string" &&
      typeof body.message === "string"
    )
      return new ApiError(
        body.code,
        body.message,
        body.request_id || "",
        error.response?.status,
      );
    return new ApiError(
      error.code === "ECONNABORTED" ? "REQUEST_TIMEOUT" : "NETWORK_ERROR",
      error.response
        ? `请求失败（${error.response.status}）`
        : "暂时无法连接服务，请检查网络或后端状态。",
      "",
      error.response?.status,
    );
  }
  return new ApiError(
    "CLIENT_ERROR",
    error instanceof Error ? error.message : "操作失败，请重试。",
  );
}
export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1"
).replace(/\/$/, "");
export const client = axios.create({ baseURL: API_BASE_URL, timeout: 45000 });
client.interceptors.response.use(
  (response) => {
    response.data = response.data.data;
    return response;
  },
  (error) => Promise.reject(normalizeError(error)),
);
export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return (await client.get<T>(path, { signal, timeout: 5000 })).data;
}
export async function post<T>(path: string, data?: unknown): Promise<T> {
  return (await client.post<T>(path, data)).data;
}
export function apiUrl(path: string): string {
  const base = new URL(API_BASE_URL, window.location.origin);
  return new URL(
    path.startsWith("/api/v1/")
      ? path
      : `${base.pathname}/${path.replace(/^\//, "")}`,
    base.origin,
  ).href;
}
export function fileDownloadUrl(fileId: string): string {
  return apiUrl(`/files/${encodeURIComponent(fileId)}/download`);
}
