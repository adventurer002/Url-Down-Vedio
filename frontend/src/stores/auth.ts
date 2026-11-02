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
  restoring: true,
  restoreError: "",
};
const listeners = new Set<() => void>();
let revision = 0;
let restoreAttempt = 0;
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
    if (state.restoring || state.restoreError) return false;
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
    update({
      token: result.access_token,
      user: result.user,
      modalOpen: false,
      message: "",
      restoring: false,
      restoreError: "",
    });
  },
  clear(message = "") {
    revision++;
    try {
      localStorage.removeItem("videohub.auth.token");
    } catch {
      /* Storage may be disabled. */
    }
    update({
      token: "",
      user: null,
      message,
      restoring: false,
      restoreError: "",
    });
  },
  async restore() {
    const expected = revision;
    const attempt = ++restoreAttempt;
    update({ restoring: true, restoreError: "" });
    try {
      const user = await get<AuthUser>("/auth/me");
      if (expected === revision && attempt === restoreAttempt)
        update({ user, restoring: false, restoreError: "" });
    } catch (error) {
      if (expected !== revision || attempt !== restoreAttempt) return;
      const failure = normalizeError(error);
      if (failure.status === 401) authStore.clear();
      else update({ restoring: false, restoreError: failure.message });
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
