import { onScopeDispose } from "vue";
import { api } from "../api";
import { apiUrl, normalizeError } from "../api/client";
import type { TaskEvent, TaskSnapshot, TaskStatus } from "../types/api";
export const terminal = (status: TaskStatus) =>
  ["completed", "failed", "cancelled", "expired"].includes(status);
interface EventConnection {
  addEventListener(type: string, listener: (event: MessageEvent) => void): void;
  close(): void;
  onerror: ((event: Event) => void) | null;
}
interface TrackerOptions {
  eventSource?: (url: string) => EventConnection;
  fetchTask?: (id: string, signal: AbortSignal) => Promise<TaskSnapshot>;
  onEvent: (event: TaskEvent) => void;
  onSnapshot: (task: TaskSnapshot) => void;
  onError?: (error: unknown) => void;
  onMode?: (mode: "sse" | "polling" | "stopped") => void;
}
const statuses: TaskStatus[] = [
  "queued",
  "running",
  "processing",
  "completed",
  "failed",
  "cancelled",
  "expired",
];
export function validEvent(value: unknown, taskId: string): value is TaskEvent {
  const v = value as TaskEvent | null;
  return (
    !!v &&
    v.task_id === taskId &&
    Number.isSafeInteger(v.seq) &&
    v.seq >= 0 &&
    statuses.includes(v.status) &&
    Number.isFinite(v.progress) &&
    v.progress >= 0 &&
    v.progress <= 100 &&
    typeof v.stage === "string"
  );
}
export function createTaskTracker(taskId: string, options: TrackerOptions) {
  let lastSeq = -1,
    stopped = false,
    finishing = false;
  let source: EventConnection | undefined,
    timer: ReturnType<typeof setTimeout> | undefined;
  const controller = new AbortController();
  const fetchTask = options.fetchTask || api.task;
  function stop() {
    stopped = true;
    source?.close();
    if (timer) clearTimeout(timer);
    controller.abort();
    options.onMode?.("stopped");
  }
  async function load() {
    if (stopped) return;
    try {
      const task = await fetchTask(taskId, controller.signal);
      if (stopped) return;
      if (!validEvent(task, taskId)) throw new Error("任务状态格式无效");
      // A full GET carries result_payload even when its seq equals the terminal SSE frame.
      if (task.seq >= lastSeq || (task.seq === 0 && terminal(task.status))) {
        if (task.seq > lastSeq) {
          lastSeq = task.seq;
          options.onEvent(task);
        }
        options.onSnapshot(task);
        if (terminal(task.status)) {
          stop();
          return;
        }
      }
    } catch (error) {
      if (stopped) return;
      options.onError?.(error);
      if (normalizeError(error).status === 404) {
        stop();
        return;
      }
    }
    if (!stopped) timer = setTimeout(load, 1500);
  }
  function fallback() {
    if (stopped || finishing) return;
    source?.close();
    source = undefined;
    if (timer) return;
    options.onMode?.("polling");
    timer = setTimeout(() => {
      timer = undefined;
      void load();
    }, 1500);
  }
  try {
    source = (options.eventSource || ((url) => new EventSource(url)))(
      apiUrl(`/tasks/${encodeURIComponent(taskId)}/events`),
    );
    options.onMode?.("sse");
    source.addEventListener("progress", (event) => {
      if (stopped || finishing) return;
      let value: unknown;
      try {
        value = JSON.parse(event.data);
      } catch {
        fallback();
        return;
      }
      if (!validEvent(value, taskId)) {
        fallback();
        return;
      }
      if (value.seq <= lastSeq) return;
      lastSeq = value.seq;
      options.onEvent(value);
      if (terminal(value.status)) {
        finishing = true;
        source?.close();
        if (timer) {
          clearTimeout(timer);
          timer = undefined;
        }
        void load(); // Resolve the full terminal payload, with retry if GET temporarily fails.
      }
    });
    source.onerror = fallback;
    source.addEventListener("error", fallback);
  } catch {
    fallback();
  }
  return {
    stop,
    get lastSeq() {
      return lastSeq;
    },
  };
}
export function useTaskPoller(taskId: string, options: TrackerOptions) {
  const tracker = createTaskTracker(taskId, options);
  onScopeDispose(tracker.stop);
  return tracker;
}
