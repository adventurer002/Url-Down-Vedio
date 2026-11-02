import { authStore } from "../stores/auth";
import axios from "axios";
import type { ApiErrorBody } from "../types/api";
declare module "axios" {
  interface AxiosRequestConfig {
    contentIdentity?: { userId: string; token: string };
    adminIdentity?: { userId: string; token: string };
  }
}
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
export const client = axios.create({
  baseURL: API_BASE_URL,
  timeout: 45000,
  withCredentials: true,
});
client.interceptors.request.use((config) => {
  const current = authStore.getState();
  const token = current.token;
  if (
    config.contentIdentity &&
    (current.user?.id !== config.contentIdentity.userId ||
      token !== config.contentIdentity.token)
  )
    throw new ApiError(
      "SESSION_CHANGED",
      "登录身份已变化，请重新选择文件",
      "",
      409,
    );
  if (
    config.adminIdentity &&
    (current.user?.id !== config.adminIdentity.userId ||
      token !== config.adminIdentity.token ||
      !token ||
      current.user?.role !== "admin" ||
      current.user.status !== "active")
  )
    throw new ApiError(
      "ADMIN_SESSION_CHANGED",
      "登录身份已变化，本次请求尚未发送。",
      "",
      409,
    );
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});
client.interceptors.response.use(
  (response) => {
    response.data = response.data.data;
    return response;
  },
  (error) => {
    const normalized = normalizeError(error);
    const current = authStore.getState();
    const sentAuthorization = error.config?.headers?.Authorization || "";
    const currentAuthorization = current.token ? `Bearer ${current.token}` : "";
    if (
      normalized.code === "INVALID_TOKEN" &&
      sentAuthorization === currentAuthorization
    ) {
      authStore.clear(normalized.message);
      if (error.config?.url !== "/auth/me") authStore.open();
    }
    if (
      normalized.code === "AUTH_REQUIRED" &&
      error.config?.url !== "/auth/me" &&
      !current.user
    )
      authStore.open();
    return Promise.reject(normalized);
  },
);
export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return (await client.get<T>(path, { signal, timeout: 5000 })).data;
}
export async function post<T>(
  path: string,
  data?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  return (
    await (signal
      ? client.post<T>(path, data, { signal })
      : client.post<T>(path, data))
  ).data;
}
export async function del<T>(path: string): Promise<T> {
  return (await client.delete<T>(path, { timeout: 10000 })).data;
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
  return authenticatedUrl(`/files/${encodeURIComponent(fileId)}/download`);
}

export function authenticatedUrl(path: string): string {
  return apiUrl(path);
}

/** Native downloads/EventSource receive a short-lived ticket scoped to one resource. */
export async function resourceAccessUrl(
  path: string,
  signal?: AbortSignal,
): Promise<string> {
  const { user, token } = authStore.getState();
  if (!user) return apiUrl(path);
  if (!/^\/(tasks\/[^/]+\/events|files\/[^/]+\/download)$/.test(path))
    throw new ApiError("INVALID_RESOURCE_PATH", "无法打开此资源");
  const access = (
    await client.post<{ access_token: string }>(`${path}/access`, undefined, {
      signal,
      timeout: 5000,
    })
  ).data;
  const current = authStore.getState();
  if (
    signal?.aborted ||
    current.user?.id !== user.id ||
    current.token !== token
  )
    throw new ApiError("SESSION_CHANGED", "登录身份已变化，请重新打开资源");
  if (!access.access_token || access.access_token === token)
    throw new ApiError(
      "INVALID_RESOURCE_ACCESS",
      "暂时无法获取访问链接，请重试",
    );
  const url = new URL(apiUrl(path));
  url.searchParams.set("access_token", access.access_token);
  return url.href;
}
