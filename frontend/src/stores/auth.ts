import { useSyncExternalStore } from "react";
import { get, post, normalizeError } from "../api/client";

export interface AuthUser {
  id: string;
  nickname: string;
  is_subscribed: boolean;
  role: string;
  status: string;
}
export interface LoginSession {
  scene_id: string;
  mode: "mock" | "verify_code" | "parameterized_qr";
  qr_url: string;
  verify_code: string;
  expires_in: number;
}
export interface LoginResult {
  status: "pending" | "expired" | "authenticated";
  access_token?: string;
  user?: AuthUser;
}
function savedToken() {
  try {
    return localStorage.getItem("videohub.auth.token") || "";
  } catch {
    return "";
  }
}
let state = {
  token: savedToken(),
  user: null as AuthUser | null,
  modalOpen: false,
  message: "",
};
const listeners = new Set<() => void>();
let revision = 0;
function update(patch: Partial<typeof state>) {
  state = { ...state, ...patch };
  listeners.forEach((listener) => listener());
}
export const authStore = {
  getState: () => state,
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },
  hasAccess() {
    return Boolean(state.user && state.user.status === "active");
  },
  requireLogin() {
    if (authStore.hasAccess()) return true;
    authStore.open();
    return false;
  },
  open() {
    update({ modalOpen: true, message: "" });
  },
  close() {
    update({ modalOpen: false });
  },
  accept(result: LoginResult) {
    if (!result.access_token || !result.user)
      throw new Error("登录响应缺少用户凭证");
    revision++;
    try {
      localStorage.setItem("videohub.auth.token", result.access_token);
    } catch {
      /* Cookie remains available. */
    }
    update({ token: result.access_token, user: result.user, message: "" });
  },
  clear(message = "") {
    revision++;
    try {
      localStorage.removeItem("videohub.auth.token");
    } catch {
      /* Storage may be disabled. */
    }
    update({ token: "", user: null, message });
  },
  async restore() {
    const expected = revision;
    try {
      const user = await get<AuthUser>("/auth/me");
      if (expected === revision) update({ user });
    } catch (error) {
      if (expected === revision && normalizeError(error).status === 401)
        authStore.clear();
    }
  },
  async logout() {
    await post("/auth/logout");
    authStore.clear();
  },
};
export const authApi = {
  create: (signal?: AbortSignal) =>
    post<LoginSession>("/auth/wechat/sessions", undefined, signal),
  poll: (id: string, signal?: AbortSignal) =>
    get<LoginResult>(`/auth/wechat/sessions/${encodeURIComponent(id)}`, signal),
  confirm: (id: string) =>
    post(`/auth/wechat/sessions/${encodeURIComponent(id)}/mock-confirm`),
};
export function useAuth() {
  return useSyncExternalStore(
    authStore.subscribe,
    authStore.getState,
    authStore.getState,
  );
}
