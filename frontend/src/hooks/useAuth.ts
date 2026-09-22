import { useMutation } from "@tanstack/react-query";

import { ApiRequestError, apiFetch } from "../api/client";
import { fetchMe, login, register } from "../api/videos";
import { useAuthStore } from "../stores/auth";

export { ApiRequestError };

export function useLogin(onErrorCode: (code: string) => void) {
  const setSession = useAuthStore((s) => s.setSession);
  return useMutation({
    mutationFn: ({ email, password }: { email: string; password: string }) =>
      login(email, password),
    onSuccess: (data) => {
      setSession(data.access_token, data.refresh_token, data.user.email);
    },
    onError: (e) => {
      if (e instanceof ApiRequestError) onErrorCode(e.code);
    },
  });
}

export function useRegister(onErrorCode: (code: string) => void) {
  const setSession = useAuthStore((s) => s.setSession);
  return useMutation({
    mutationFn: ({
      email,
      password,
      nickname,
    }: {
      email: string;
      password: string;
      nickname?: string;
    }) => register(email, password, nickname),
    onSuccess: (data) => {
      setSession(data.access_token, data.refresh_token, data.user.email);
    },
    onError: (e) => {
      if (e instanceof ApiRequestError) onErrorCode(e.code);
    },
  });
}

export function useLogout() {
  const clear = useAuthStore((s) => s.clear);
  return () => {
    clear();
  };
}

export function useLoginState() {
  const token = useAuthStore((s) => s.accessToken);
  return { loggedIn: !!token };
}

export async function restoreSession(): Promise<void> {
  const clear = useAuthStore.getState().clear;
  try {
    await apiFetch("/api/v1/users/me");
  } catch {
    clear();
  }
}

export { fetchMe };
