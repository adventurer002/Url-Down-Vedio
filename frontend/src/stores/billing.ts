import { useSyncExternalStore } from "react";
import { api } from "../api";
import { normalizeError, type ApiError } from "../api/client";
import { authStore, useAuth } from "./auth";
import type { Allowances } from "../types/billing";
import { billingFreshUntil } from "../utils/billing";

const empty = {
  userId: "",
  data: null as Allowances | null,
  loading: false,
  error: null as ApiError | null,
  freshUntil: 0,
  usage: {} as Record<string, { asr_seconds: number; ai_generations: number }>,
};
let state = empty;
let generation = 0;
let request: AbortController | undefined;
let pending: Promise<void> | undefined;
let usagePending: Promise<void> | undefined;
let usageController: AbortController | undefined;
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
    usageController?.abort();
    usageController = undefined;
    usagePending = undefined;
    request = undefined;
    pending = undefined;
    generation++;
    update({ ...empty, userId: userId || "" });
  },
  async loadUsage(taskId: string) {
    const who = authStore.getState();
    if (!who.user || state.usage[taskId]) return;
    if (usagePending) return usagePending;
    const controller = new AbortController();
    usageController = controller;
    usagePending = (async () => {
      try {
        const ledger = await api.ledger(controller.signal);
        if (
          controller.signal.aborted ||
          authStore.getState().user?.id !== who.user?.id ||
          authStore.getState().token !== who.token
        )
          return;
        const usage = { ...state.usage };
        for (const event of ledger.items)
          if (event.event === "settled" && event.detail.task_id)
            usage[event.detail.task_id] = {
              asr_seconds: event.detail.asr_seconds || 0,
              ai_generations: event.detail.ai_generations || 0,
            };
        update({ usage });
      } catch {
        /* Older tasks may be outside the current ledger page; never invent their usage. */
      } finally {
        if (usageController === controller) {
          usagePending = undefined;
          usageController = undefined;
        }
      }
    })();
    return usagePending;
  },
  async refresh({ ifStale = false } = {}): Promise<void> {
    const userId = authStore.getState().user?.id;
    if (!userId) {
      billingStore.setUser(null);
      return;
    }
    billingStore.setUser(userId);
    if (ifStale) {
      if (pending) return pending;
      if (state.data && !state.error && Date.now() < state.freshUntil) return;
    }
    request?.abort();
    const controller = new AbortController();
    request = controller;
    const expected = ++generation;
    update({ loading: true, error: null });
    pending = (async () => {
      try {
        const data = await api.allowances(controller.signal);
        if (expected === generation && authStore.getState().user?.id === userId)
          update({ data, freshUntil: billingFreshUntil(data, Date.now()) });
      } catch (error) {
        if (expected === generation && !controller.signal.aborted)
          update({ error: normalizeError(error) });
      } finally {
        if (expected === generation) {
          request = undefined;
          pending = undefined;
          update({ loading: false });
        }
      }
    })();
    return pending;
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
