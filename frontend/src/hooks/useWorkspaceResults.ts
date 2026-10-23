import { useAuth } from "../stores/auth";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import type {
  AIResult,
  MindMapResult,
  SummaryResult,
  TranscriptResult,
} from "../types/api";
import { useWorkspace } from "./useWorkspace";

export function useWorkspaceResults(videoKey: string) {
  const store = useWorkspace();
  const auth = useAuth();
  const identity = auth.user?.id || "";
  const cache = useRef(new Map<string, AIResult | TranscriptResult>());
  const [summary, setSummary] = useState<SummaryResult>();
  const [transcript, setTranscript] = useState<TranscriptResult>();
  const [mindmap, setMindmap] = useState<MindMapResult>();
  const [error, setError] = useState<ApiError | null>(null);
  const [attempt, setAttempt] = useState(0);
  const signature = store.tasks
    .filter((task) => task.videoKey === videoKey)
    .map(
      (task) =>
        `${task.id}:${task.event.status}:${task.snapshot?.result_payload?.result_id || task.snapshot?.result_payload?.transcript_id || ""}`,
    )
    .join("|");
  useEffect(() => {
    cache.current.clear();
    setSummary(undefined);
    setTranscript(undefined);
    setMindmap(undefined);
    setError(null);
  }, [identity]);
  useEffect(() => {
    let disposed = false;
    const controller = new AbortController();
    async function load() {
      setError(null);
      const completed = store.tasks.filter(
        (task) =>
          task.videoKey === videoKey && task.event.status === "completed",
      );
      const candidates = completed.filter(
        (task, i) =>
          completed.findIndex((other) => other.kind === task.kind) === i,
      );
      let nextTranscript: TranscriptResult | undefined;
      try {
        async function getTranscript(id: string) {
          let value = cache.current.get(id) as TranscriptResult | undefined;
          if (!value) {
            value = await api.transcript(id, controller.signal);
            if (!disposed) cache.current.set(id, value);
          }
          return value;
        }
        for (const task of candidates.reverse()) {
          const payload = task.snapshot?.result_payload;
          if (!payload || disposed) continue;
          if (payload.transcript_id) {
            nextTranscript = await getTranscript(payload.transcript_id);
            if (disposed) return;
            setTranscript(nextTranscript);
          }
          if (payload.result_id) {
            let result = cache.current.get(payload.result_id) as
              | AIResult
              | undefined;
            if (!result) {
              result = await api.result(payload.result_id, controller.signal);
              if (disposed) return;
              cache.current.set(payload.result_id, result);
            }
            if (disposed) return;
            if (result.result_type === "summary") setSummary(result);
            else setMindmap(result);
            if (nextTranscript?.id !== result.transcript_id) {
              nextTranscript = await getTranscript(result.transcript_id);
              if (disposed) return;
              setTranscript(nextTranscript);
            }
          }
        }
      } catch (e) {
        if (!disposed) setError(normalizeError(e));
      }
    }
    void load();
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [store, videoKey, signature, attempt, identity]);
  return {
    summary,
    transcript,
    mindmap,
    resultError: error,
    reloadResults: () => setAttempt((value) => value + 1),
  };
}
