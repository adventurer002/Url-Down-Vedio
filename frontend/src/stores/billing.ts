import { useSyncExternalStore } from "react";
import { api } from "../api";
import { normalizeError, type ApiError } from "../api/client";
import { authStore, useAuth } from "./auth";
import type { Allowances } from "../types/billing";

const empty = {
  userId: "",
  data: null as Allowances | null,
  loading: false,
  error: null as ApiError | null,
};
let state = empty;
let generation = 0;
let request: AbortController | undefined;
const listeners = new Set<() => void>();
function update(patch: Partial<typeof state>) {
  state = { ...state, ...patch };
  listeners.forEach((listener) => listener());
}
export const billingStore = {
  getState: () => state,
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },
  setUser(userId: string | null) {
    if ((userId || "") === state.userId) return;
    request?.abort();
    generation++;
    update({ ...empty, userId: userId || "" });
  },
  async refresh() {
    const userId = authStore.getState().user?.id;
    if (!userId) {
      billingStore.setUser(null);
      return;
    }
    billingStore.setUser(userId);
    request?.abort();
    const controller = new AbortController();
    request = controller;
    const expected = ++generation;
    update({ loading: true, error: null });
    try {
      const data = await api.allowances(controller.signal);
      if (expected === generation && authStore.getState().user?.id === userId)
        update({ data });
    } catch (error) {
      if (expected === generation && !controller.signal.aborted)
        update({ error: normalizeError(error) });
    } finally {
      if (expected === generation) update({ loading: false });
    }
  },
};
export function useBilling() {
  const { user } = useAuth();
  const snapshot = useSyncExternalStore(
    billingStore.subscribe,
    billingStore.getState,
    billingStore.getState,
  );
  return snapshot.userId === (user?.id || "") ? snapshot : empty;
}
