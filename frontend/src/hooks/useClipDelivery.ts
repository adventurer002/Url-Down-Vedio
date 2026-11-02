import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { ApiError } from "../api/client";
import {
  assertClipExport,
  clipError,
  compareClipAnalyses,
  type ClipAnalysisOrder,
} from "../lib/clips";
import { authStore, useAuth } from "../stores/auth";
import type { ClipExport, ClipPlan } from "../types/clips";
import { useWorkspace } from "./useWorkspace";

type DeliveryState = {
  owner: string;
  delivery?: ClipExport;
  loading: boolean;
  error: ApiError | null;
};
/** Resource reads only. The global workspace owns all export task tracking. */
export function useClipDelivery(
  plan: ClipPlan,
  videoKey: string,
  initial: ClipExport | undefined,
  active: boolean,
) {
  const store = useWorkspace(),
    auth = useAuth();
  const owner = JSON.stringify([
    auth.user?.id,
    auth.token,
    videoKey,
    plan.id,
    plan.revision,
    initial?.id,
  ]);
  const currentOwner = useRef(owner);
  currentOwner.current = owner;
  const [state, setState] = useState<DeliveryState>({
    owner,
    loading: false,
    error: null,
  });
  const [attempt, setAttempt] = useState(0);
  const current = useRef<ClipExport | undefined>(undefined);
  const seen = useRef<ClipAnalysisOrder | undefined>(undefined);
  const initialised = useRef(false);
  const lastRead = useRef<string | undefined>(undefined);
  const alive = useRef(true);
  const completed = store.tasks
    .filter(
      (task) =>
        task.kind === "clips_export" &&
        task.event.status === "completed" &&
        task.snapshot?.result_payload?.clip_plan_id === plan.id &&
        task.snapshot.result_payload.clip_export_id &&
        (task.videoKey === videoKey ||
          task.snapshot.video_id === plan.video_id),
    )
    .sort((a, b) => compareClipAnalyses(b, a));
  const latest = completed[0],
    latestId = latest?.snapshot?.result_payload?.clip_export_id;
  const saved = store
    .findContent(videoKey)
    ?.clipExports?.find((entry) => entry.planId === plan.id)?.exportId;
  const userId = auth.user?.id,
    token = auth.token;
  const valid = (expected: string, signal: AbortSignal) =>
    !signal.aborted &&
    alive.current &&
    currentOwner.current === expected &&
    authStore.getState().user?.id === userId &&
    authStore.getState().token === token &&
    store.userId === userId;
  useEffect(() => {
    current.current = undefined;
    seen.current = undefined;
    initialised.current = false;
    lastRead.current = undefined;
    alive.current = true;
    setState({ owner, loading: false, error: null });
    return () => {
      alive.current = false;
    };
  }, [owner]);
  useEffect(() => {
    if (!active) {
      lastRead.current = undefined;
      return;
    }
    if (
      !active ||
      !userId ||
      auth.restoring ||
      auth.restoreError ||
      store.userId !== userId
    )
      return;
    const controller = new AbortController();
    async function load() {
      let id = current.current?.id;
      if (!initialised.current) id = initial?.id || latestId || saved;
      else if (
        latest &&
        latestId &&
        (!seen.current || compareClipAnalyses(latest, seen.current) > 0)
      )
        id = latestId;
      if (!id) return;
      const readKey = JSON.stringify([
        owner,
        attempt,
        latest?.id,
        latestId,
        id,
      ]);
      if (current.current?.id === id && lastRead.current === readKey) return;
      setState((previous) => ({
        ...previous,
        owner,
        loading: true,
        error: null,
      }));
      try {
        const value =
          !initialised.current && !attempt && initial?.id === id
            ? initial
            : await api.clipExport(id, controller.signal);
        if (!valid(owner, controller.signal)) return;
        if (value.id !== id)
          throw new ApiError(
            "CLIP_RESPONSE_INVALID",
            "切片成品编号不一致，请重新加载",
          );
        assertClipExport(value, plan);
        current.current = value;
        seen.current = latest;
        initialised.current = true;
        lastRead.current = readKey;
        store.rememberClipExport(videoKey, plan.id, value.id);
        setState({ owner, delivery: value, loading: false, error: null });
      } catch (cause) {
        if (valid(owner, controller.signal))
          setState((previous) => ({
            ...previous,
            owner,
            loading: false,
            error: clipError(cause),
          }));
      }
    }
    void load();
    return () => controller.abort();
  }, [
    owner,
    active,
    userId,
    token,
    auth.restoring,
    auth.restoreError,
    store.userId,
    latest?.id,
    latestId,
    saved,
    attempt,
  ]);
  useEffect(() => {
    if (!active) return;
    const refresh = () => {
      if (document.visibilityState === "visible")
        setAttempt((value) => value + 1);
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [active]);
  const visible =
    state.owner === owner ? state : { owner, loading: false, error: null };
  return {
    ...visible,
    latest,
    reload: () => setAttempt((value) => value + 1),
    async check(signal: AbortSignal) {
      const id = visible.delivery?.id;
      if (!id)
        throw new ApiError("CLIP_EXPORT_NOT_FOUND", "请重新加载切片成品");
      const value = await api.clipExport(id, signal);
      if (!valid(owner, signal) || current.current?.id !== id)
        throw new ApiError(
          "AUTH_CHANGED",
          "登录身份或方案已变化，请重新打开成品",
        );
      if (value.id !== id)
        throw new ApiError(
          "CLIP_RESPONSE_INVALID",
          "切片成品编号不一致，请重新加载",
        );
      assertClipExport(value, plan);
      current.current = value;
      setState((previous) => ({ ...previous, delivery: value }));
      return value;
    },
  };
}
