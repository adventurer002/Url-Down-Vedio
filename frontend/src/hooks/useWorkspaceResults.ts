import { useAuth } from "../stores/auth";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { normalizeError, type ApiError } from "../api/client";
import type {
  AIResult,
  MindMapResult,
  SummaryResult,
  TranscriptResult,
} from "../types/api";
import { useWorkspaceStore, useWorkspaceValue } from "./useWorkspace";

type Bundle = {
  transcript?: TranscriptResult;
  summary?: SummaryResult;
  mindmap?: MindMapResult;
};
export function useWorkspaceResults(
  videoKey: string,
  requestedTranscript?: string | null,
  requestedResult?: string | null,
) {
  const store = useWorkspaceStore(),
    auth = useAuth();
  const identity = auth.user?.id || "";
  const ownerKey = [
    identity,
    videoKey,
    requestedTranscript || "",
    requestedResult || "",
  ].join(":");
  const [owner, setOwner] = useState(ownerKey),
    [bundle, setBundle] = useState<Bundle>({});
  const [pending, setPending] = useState<Bundle>(),
    [error, setError] = useState<ApiError | null>(null);
  const [attempt, setAttempt] = useState(0),
    [loading, setLoading] = useState(false);
  const current = useRef<Bundle>({}),
    cache = useRef(new Map<string, AIResult | TranscriptResult>());
  const signature = useWorkspaceValue((workspace) =>
    JSON.stringify({
      saved: workspace.findContent(videoKey)?.results,
      tasks: workspace.tasks
        .filter((task) => task.videoKey === videoKey)
        .map((task) => [
          task.id,
          task.event.status,
          task.snapshot?.result_payload,
        ]),
    }),
  );
  useEffect(() => {
    setOwner(ownerKey);
    cache.current.clear();
    current.current = {};
    setBundle({});
    setPending(undefined);
    setError(null);
  }, [ownerKey]);
  useEffect(() => {
    if (auth.restoring || auth.restoreError) return;
    let disposed = false;
    const controller = new AbortController();
    function publish(next: Bundle) {
      if (disposed) return;
      const previous = current.current;
      const changed =
        (previous.transcript &&
          next.transcript &&
          previous.transcript.id !== next.transcript.id) ||
        (previous.summary &&
          next.summary &&
          previous.summary.id !== next.summary.id) ||
        (previous.mindmap &&
          next.mindmap &&
          previous.mindmap.id !== next.mindmap.id);
      if (changed) {
        setPending(next);
        return;
      }
      const combined = { ...previous, ...next };
      current.current = combined;
      setBundle(combined);
    }
    async function result(id: string) {
      let value = cache.current.get(id) as AIResult | undefined;
      if (!value) {
        value = await api.result(id, controller.signal);
        if (!disposed) cache.current.set(id, value);
      }
      return value;
    }
    async function transcript(id: string) {
      let value = cache.current.get(id) as TranscriptResult | undefined;
      if (!value) {
        value = await api.transcript(id, controller.signal);
        if (!disposed) cache.current.set(id, value);
      }
      return value;
    }
    async function load() {
      setError(null);
      setLoading(true);
      try {
        const completed = store.tasks.filter(
          (task) =>
            task.videoKey === videoKey &&
            task.event.status === "completed" &&
            !["ai_question", "ai_clip_plan", "clips_export"].includes(
              task.kind,
            ),
        );
        const saved = store.findContent(videoKey)?.results;
        const explicit = requestedResult
          ? await result(requestedResult)
          : undefined;
        let target =
          requestedTranscript ||
          explicit?.transcript_id ||
          completed.find((task) => task.snapshot?.result_payload?.transcript_id)
            ?.snapshot?.result_payload?.transcript_id ||
          saved?.transcriptId;
        const references = [
          ...new Set(
            [
              requestedResult,
              ...completed.flatMap((task) => [
                task.snapshot?.result_payload?.result_id,
                task.snapshot?.result_payload?.summary_result_id,
              ]),
              saved?.summaryId,
              saved?.mindmapId,
            ].filter((id): id is string => !!id),
          ),
        ];
        if (!target && references.length)
          target = (await result(references[0])).transcript_id;
        if (disposed || !target) return;
        const text = await transcript(target);
        if (disposed) return;
        publish({ transcript: text });
        const next: Bundle = { transcript: text };
        const language = explicit?.output_language;
        for (const id of references) {
          if (disposed) return;
          try {
            const value = await result(id);
            if (
              value.transcript_id !== target ||
              (language && value.output_language !== language)
            )
              continue;
            if (value.result_type === "summary" && !next.summary)
              next.summary = value;
            else if (value.result_type === "mindmap" && !next.mindmap)
              next.mindmap = value;
          } catch (cause) {
            if (!disposed) setError(normalizeError(cause));
          }
          if (next.summary && next.mindmap) break;
        }
        publish(next);
      } catch (cause) {
        if (!disposed) setError(normalizeError(cause));
      } finally {
        if (!disposed) setLoading(false);
      }
    }
    void load();
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [
    store,
    videoKey,
    signature,
    attempt,
    ownerKey,
    requestedTranscript,
    requestedResult,
    auth.restoring,
    auth.restoreError,
  ]);
  const visible = owner === ownerKey ? bundle : {};
  return {
    ...visible,
    resultError: owner === ownerKey ? error : null,
    loading,
    hasNewResults: owner === ownerKey && !!pending,
    acceptNewResults() {
      if (pending) {
        current.current = pending;
        setBundle(pending);
        setPending(undefined);
      }
    },
    reloadResults() {
      cache.current.clear();
      setAttempt((value) => value + 1);
    },
  };
}
