import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { effectScope } from "vue";
import { createTaskTracker, useTaskPoller } from "../composables/useTaskPoller";
import type { TaskEvent, TaskSnapshot } from "../types/api";
import { ApiError } from "../api/client";
class FakeSource {
  onerror: ((event: Event) => void) | null = null;
  listeners = new Map<string, (event: MessageEvent) => void>();
  close = vi.fn();
  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, listener);
  }
  emit(value: unknown) {
    this.listeners.get("progress")?.({
      data: JSON.stringify(value),
    } as MessageEvent);
  }
}
const event = (
  seq: number,
  status: TaskEvent["status"] = "running",
): TaskEvent => ({
  task_id: "task-1",
  seq,
  status,
  progress: status === "completed" ? 100 : 30,
  stage: "summary",
  error_code: null,
  updated_at: "2026-10-21",
});
const snapshot = (seq: number, status: TaskEvent["status"] = "completed") =>
  ({
    ...event(seq, status),
    result_payload: { result_id: "result-1" },
  }) as TaskSnapshot;
beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("window", { location: { origin: "http://localhost:5173" } });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
function setup(fetchTask = vi.fn().mockResolvedValue(snapshot(5))) {
  const source = new FakeSource(),
    onEvent = vi.fn(),
    onSnapshot = vi.fn(),
    onError = vi.fn();
  const tracker = createTaskTracker("task-1", {
    eventSource: () => source,
    fetchTask,
    onEvent,
    onSnapshot,
    onError,
  });
  return { source, onEvent, onSnapshot, onError, tracker, fetchTask };
}
describe("task SSE and polling", () => {
  it("strictly discards repeated or out-of-order seq and hydrates same-seq terminal payload", async () => {
    const s = setup();
    s.source.emit(event(2));
    s.source.emit(event(2));
    s.source.emit(event(1));
    s.source.emit(event(5, "completed"));
    await vi.advanceTimersByTimeAsync(0);
    expect(s.onEvent.mock.calls.map(([value]) => value.seq)).toEqual([2, 5]);
    expect(s.onSnapshot).toHaveBeenCalledWith(snapshot(5));
    expect(s.source.close).toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(10000);
    expect(s.fetchTask).toHaveBeenCalledTimes(1);
  });
  it("falls back to 1.5 second polling and stops on terminal status", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(snapshot(2, "running"))
      .mockResolvedValueOnce(snapshot(3));
    const s = setup(fetcher);
    s.source.onerror?.({} as Event);
    await vi.advanceTimersByTimeAsync(1499);
    expect(fetcher).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(fetcher).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1500);
    expect(fetcher).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(6000);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(s.onSnapshot.mock.calls.at(-1)?.[0].status).toBe("completed");
  });
  it.each(["completed", "failed", "cancelled", "expired"] as const)(
    "closes on %s",
    async (status) => {
      const s = setup(vi.fn().mockResolvedValue(snapshot(3, status)));
      s.source.emit(event(3, status));
      await vi.advanceTimersByTimeAsync(0);
      expect(s.onSnapshot).toHaveBeenCalledTimes(1);
      expect(s.source.close).toHaveBeenCalled();
    },
  );
  it("never regresses to an older polled event", async () => {
    const s = setup(
      vi
        .fn()
        .mockResolvedValueOnce(snapshot(2, "running"))
        .mockResolvedValueOnce(snapshot(6)),
    );
    s.source.emit(event(5));
    s.source.onerror?.({} as Event);
    await vi.advanceTimersByTimeAsync(1500);
    expect(s.onSnapshot).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1500);
    expect(s.tracker.lastSeq).toBe(6);
  });
  it("retries fetching terminal results after temporary GET failure", async () => {
    const s = setup(
      vi
        .fn()
        .mockRejectedValueOnce(new Error("offline"))
        .mockResolvedValue(snapshot(5)),
    );
    s.source.emit(event(5, "completed"));
    await vi.advanceTimersByTimeAsync(0);
    expect(s.onError).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1500);
    expect(s.onSnapshot).toHaveBeenCalledTimes(1);
  });
  it("stops polling if the task does not exist", async () => {
    const s = setup(
      vi
        .fn()
        .mockRejectedValue(new ApiError("TASK_NOT_FOUND", "gone", "", 404)),
    );
    s.source.onerror?.({} as Event);
    await vi.advanceTimersByTimeAsync(15000);
    expect(s.fetchTask).toHaveBeenCalledTimes(1);
  });
  it("validates task identity and falls back on malformed events", async () => {
    const s = setup();
    s.source.emit({ ...event(999), task_id: "other" });
    expect(s.onEvent).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1500);
    expect(s.fetchTask).toHaveBeenCalledTimes(1);
  });
  it("uses durable terminal state when Redis is unavailable and GET returns seq zero", async () => {
    const s = setup(vi.fn().mockResolvedValue(snapshot(0)));
    s.source.emit(event(7));
    s.source.onerror?.({} as Event);
    await vi.advanceTimersByTimeAsync(1500);
    expect(s.onSnapshot).toHaveBeenCalledWith(snapshot(0));
    expect(s.onEvent).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(5000);
    expect(s.fetchTask).toHaveBeenCalledTimes(1);
  });
  it("scope disposal closes the source, aborts I/O and cancels timers", async () => {
    const source = new FakeSource(),
      fetchTask = vi.fn().mockResolvedValue(snapshot(1));
    const scope = effectScope();
    scope.run(() =>
      useTaskPoller("task-1", {
        eventSource: () => source,
        fetchTask,
        onEvent: vi.fn(),
        onSnapshot: vi.fn(),
      }),
    );
    source.onerror?.({} as Event);
    scope.stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(source.close).toHaveBeenCalled();
    expect(fetchTask).not.toHaveBeenCalled();
  });
});
