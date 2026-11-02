import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { ApiError } from "../api/client";
import {
  assertClipContent,
  clipError,
  compareClipAnalyses,
  isClipTask,
  latestClipAnalysis,
  type ClipAnalysisOrder,
} from "../lib/clips";
import { authStore, useAuth } from "../stores/auth";
import type { ClipPlan, ClipExport } from "../types/clips";
import type { LocalTask } from "../stores/workspace";
import { useWorkspace } from "./useWorkspace";

type State = {
  owner: string;
  plan?: ClipPlan;
  pending?: ClipPlan;
  pendingAnalysis?: LocalTask;
  exported?: ClipExport;
  loading: boolean;
  error: ApiError | null;
  task?: LocalTask;
  contentKey?: string;
};
/** Read-only restoration. Task execution and SSE remain owned by the global workspace. */
export function useClipWorkspace(
  videoKey: string,
  requested: {
    plan?: string | null;
    exported?: string | null;
    task?: string | null;
  },
  enabled: boolean,
) {
  const store = useWorkspace(),
    auth = useAuth();
  const userId = auth.user?.id,
    token = auth.token;
  const owner = JSON.stringify([
    userId,
    token,
    videoKey,
    requested.plan,
    requested.exported,
    requested.task,
  ]);
  const [state, setState] = useState<State>({
    owner,
    loading: false,
    error: null,
  });
  const [attempt, setAttempt] = useState(0);
  const currentOwner = useRef(owner),
    acceptedAnalysis = useRef<ClipAnalysisOrder | undefined>(undefined);
  currentOwner.current = owner;
  const current = useRef<ClipPlan | undefined>(undefined),
    cache = useRef(new Map<string, ClipPlan>());
  const video = store.findContent(videoKey);
  const relevant = store.tasks.filter(
    (task) =>
      isClipTask(task.kind) &&
      (task.id === requested.task ||
        task.videoKey === videoKey ||
        (!!video?.contentId && task.snapshot?.video_id === video.contentId)),
  );
  const signature = JSON.stringify(
    relevant.map((task) => [
      task.id,
      task.kind,
      task.createdAt,
      task.snapshot?.created_at,
      task.event.status,
      task.snapshot?.result_payload,
    ]),
  );
  const latest = latestClipAnalysis(
    relevant.filter(
      (task) =>
        task.event.status === "completed" &&
        task.snapshot?.result_payload?.clip_plan_id,
    ),
  );
  const latestPlanId = latest?.snapshot?.result_payload?.clip_plan_id;
  const savedId = video?.clipPlanId;
  const storeUserId = store.userId;
  useEffect(() => {
    current.current = undefined;
    acceptedAnalysis.current = undefined;
    cache.current.clear();
    setState({ owner, loading: enabled, error: null });
  }, [owner]);
  useEffect(() => {
    if (
      !enabled ||
      !userId ||
      auth.restoring ||
      auth.restoreError ||
      storeUserId !== userId
    )
      return;
    const controller = new AbortController();
    let disposed = false;
    const valid = () =>
      currentOwner.current === owner &&
      !disposed &&
      !controller.signal.aborted &&
      authStore.getState().user?.id === userId &&
      authStore.getState().token === token &&
      store.userId === userId;
    const update = (value: Partial<State>) => {
      if (valid())
        setState((previous) => ({
          ...(previous.owner === owner
            ? previous
            : { owner, error: null, loading: false }),
          ...value,
        }));
    };
    async function readPlan(id: string) {
      const cached = cache.current.get(id);
      if (cached) return cached;
      const plan = await api.clipPlan(id, controller.signal);
      if (!valid()) return;
      if (plan.id !== id)
        throw new ApiError(
          "CLIP_RESPONSE_INVALID",
          "切片方案编号不一致，请重新加载",
        );
      assertClipContent(plan, store.findContent(videoKey));
      cache.current.set(id, plan);
      if (cache.current.size > 10)
        cache.current.delete(cache.current.keys().next().value!);
      return plan;
    }
    async function load() {
      update({
        loading: true,
        error: null,
        pending: undefined,
        pendingAnalysis: undefined,
      });
      try {
        let target = requested.plan || undefined;
        let linkedExport: ClipExport | undefined;
        let expectedVideo: string | null | undefined;
        if (requested.exported) {
          const exported = await api.clipExport(
            requested.exported,
            controller.signal,
          );
          if (!valid()) return;
          if (
            exported.id !== requested.exported ||
            (target && target !== exported.plan_id)
          )
            throw new ApiError(
              "CLIP_CONTENT_MISMATCH",
              "切片结果与方案不一致，请从任务历史重新打开",
            );
          target = exported.plan_id;
          linkedExport = exported;
        }
        if (requested.task) {
          const known = store.tasks.find((item) => item.id === requested.task);
          const snapshot =
            known?.snapshot && known.snapshot.status === known.event.status
              ? known.snapshot
              : await api.task(requested.task, controller.signal);
          if (!valid()) return;
          if (
            snapshot.task_id !== requested.task ||
            !isClipTask(snapshot.task_type)
          )
            throw new ApiError("CLIP_TASK_INVALID", "此链接不是切片任务");
          const existing = store.findContent(videoKey);
          if (
            existing?.contentId &&
            snapshot.video_id &&
            existing.contentId !== snapshot.video_id
          )
            throw new ApiError(
              "CLIP_CONTENT_MISMATCH",
              "任务与当前内容不一致，请从任务历史重新打开",
            );
          expectedVideo = snapshot.video_id;
          const task = store.adoptClipTask(
            snapshot,
            existing?.key || `content_${snapshot.video_id}`,
          );
          update({ task });
          const result = snapshot.result_payload;
          if (snapshot.status !== "completed") return;
          if (result?.clip_export_id) {
            const exported = await api.clipExport(
              result.clip_export_id,
              controller.signal,
            );
            if (!valid()) return;
            if (
              exported.id !== result.clip_export_id ||
              (result.clip_plan_id &&
                result.clip_plan_id !== exported.plan_id) ||
              (target && target !== exported.plan_id)
            )
              throw new ApiError("CLIP_CONTENT_MISMATCH", "任务与方案不一致");
            target = exported.plan_id;
            linkedExport = exported;
          } else if (result?.clip_plan_id) {
            if (target && target !== result.clip_plan_id)
              throw new ApiError("CLIP_CONTENT_MISMATCH", "任务与方案不一致");
            target = result.clip_plan_id;
          } else
            throw new ApiError(
              "CLIP_RESPONSE_INVALID",
              "任务结果缺少切片方案，请重新加载",
            );
        }
        target ||= latestPlanId || savedId;
        if (!target) return;
        const plan = await readPlan(target);
        if (!valid() || !plan) return;
        assertClipContent(plan, store.findContent(videoKey));
        if (expectedVideo && plan.video_id !== expectedVideo)
          throw new ApiError(
            "CLIP_CONTENT_MISMATCH",
            "任务结果与当前内容不一致",
          );
        if (linkedExport) update({ exported: linkedExport });
        const reading = current.current
          ? await readPlan(current.current.id)
          : plan;
        if (!valid() || !reading) return;
        if (current.current !== reading) {
          current.current = reading;
          const contentKey = store.rememberClipPlan(reading);
          update({ plan: reading, contentKey });
        }
        // Offer the latest analysis, even when it returns an older cached artifact.
        // Accepted task order prevents a late older completion from rolling it back.
        if (
          latest &&
          latestPlanId !== current.current.id &&
          !acceptedAnalysis.current
        ) {
          const known = latestClipAnalysis(
            relevant.filter(
              (task) =>
                task.snapshot?.result_payload?.clip_plan_id ===
                current.current!.id,
            ),
          );
          if (known) acceptedAnalysis.current = known;
          else {
            // A deep-linked plan may be newer than the local task history. Resolve its
            // original request, rather than using the artifact's creation timestamp.
            const original = await api.task(
              current.current.task_id,
              controller.signal,
            );
            if (!valid()) return;
            if (
              original.task_id !== current.current.task_id ||
              original.task_type !== "ai_clip_plan" ||
              original.video_id !== current.current.video_id ||
              original.result_payload?.clip_plan_id !== current.current.id
            )
              throw new ApiError(
                "CLIP_TASK_INVALID",
                "方案与分析任务不一致，请重新加载",
              );
            acceptedAnalysis.current = {
              id: original.task_id,
              createdAt: original.created_at,
              snapshot: original,
            };
          }
        }
        if (
          latest &&
          latestPlanId &&
          (!acceptedAnalysis.current ||
            compareClipAnalyses(latest, acceptedAnalysis.current) >= 0)
        ) {
          if (latestPlanId === current.current.id)
            acceptedAnalysis.current = latest;
          else {
            const next = await readPlan(latestPlanId);
            if (valid() && next && next.id !== current.current.id)
              update({ pending: next, pendingAnalysis: latest });
          }
        }
      } catch (cause) {
        if (valid()) update({ error: clipError(cause) });
      } finally {
        update({ loading: false });
      }
    }
    void load();
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [
    owner,
    enabled,
    userId,
    token,
    auth.restoring,
    auth.restoreError,
    storeUserId,
    signature,
    latest?.id,
    latestPlanId,
    savedId,
    attempt,
    store,
    videoKey,
    requested.plan,
    requested.exported,
    requested.task,
  ]);
  const visible =
    state.owner === owner ? state : { owner, loading: enabled, error: null };
  return {
    ...visible,
    loading:
      enabled &&
      !!userId &&
      !auth.restoreError &&
      (auth.restoring || visible.loading),
    reload() {
      cache.current.clear();
      setAttempt((value) => value + 1);
    },
    acceptPending() {
      if (
        !visible.pending ||
        visible.pendingAnalysis?.id !== latest?.id ||
        authStore.getState().user?.id !== userId ||
        authStore.getState().token !== token
      )
        return;
      const plan = visible.pending;
      current.current = plan;
      acceptedAnalysis.current = visible.pendingAnalysis;
      const contentKey = store.rememberClipPlan(plan);
      setState({
        owner,
        plan,
        contentKey,
        pending: undefined,
        error: null,
        loading: false,
      });
      return plan;
    },
  };
}
export type ClipWorkspaceResource = ReturnType<typeof useClipWorkspace>;
