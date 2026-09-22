import { create } from "zustand";

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  email: string | null;
  setSession: (access: string, refresh: string, email: string) => void;
  clear: () => void;
}

function readToken(): string | null {
  return localStorage.getItem("access_token");
}

export const useAuthStore = create<AuthState>((set) => ({
  accessToken: readToken(),
  refreshToken: localStorage.getItem("refresh_token"),
  email: localStorage.getItem("email"),
  setSession: (access, refresh, email) => {
    localStorage.setItem("access_token", access);
    localStorage.setItem("refresh_token", refresh);
    localStorage.setItem("email", email);
    set({ accessToken: access, refreshToken: refresh, email });
  },
  clear: () => {
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    localStorage.removeItem("email");
    set({ accessToken: null, refreshToken: null, email: null });
  },
}));
