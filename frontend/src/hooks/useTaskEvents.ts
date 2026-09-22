import { useEffect } from "react";

import { isTerminal } from "./useVideos";
import type { TaskStatus } from "../types";

export interface SseEvent {
  status: TaskStatus;
  progress: number;
  mediaFileId?: string | null;
  end?: boolean;
}

/**
 * Listen to the task SSE feed. Reconnects automatically after the stream
 * closes for a non-terminal task; durable state is replayed from the DB on
 * each (re)connect, so progress never resets.
 */
export function useTaskEvents(
  taskId: string | null,
  onEvent: (event: SseEvent) => void,
): void {
  useEffect(() => {
    if (!taskId) return;
    let stopped = false;
    let retryMs = 500;

    const open = () => {
      return new Promise<void>((resolve) => {
        const src = new EventSource(`/api/v1/downloads/${taskId}/events`);
        let done = false;
        const finish = () => {
          if (done) return;
          done = true;
          src.close();
          resolve();
        };

        src.addEventListener("progress", (e) => {
          const data = JSON.parse((e as MessageEvent).data) as SseEvent;
          onEvent(data);
          if (isTerminal(data.status) || data.end) finish();
        });
        src.addEventListener("end", (e) => {
          const data = JSON.parse((e as MessageEvent).data) as SseEvent;
          onEvent({ status: data.status, progress: 100, mediaFileId: data.mediaFileId, end: true });
          finish();
        });
        src.onerror = () => {
          if (!done) {
            retryMs = Math.min(retryMs * 2, 8000);
            src.close();
          }
          finish();
        };
      });
    };

    const loop = async () => {
      while (!stopped && taskId) {
        await open();
        if (stopped) break;
        await new Promise((r) => setTimeout(r, retryMs));
      }
    };

    void loop();
    return () => {
      stopped = true;
    };
  }, [taskId, onEvent]);
}