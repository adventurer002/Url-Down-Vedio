// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { authStore } from "../stores/auth";
import { createWorkspace, type LocalTask } from "../stores/workspace";
import { WorkspaceProvider } from "../hooks/useWorkspace";
import { useWorkspaceResults } from "../hooks/useWorkspaceResults";
import type {
  AIResult,
  TranscriptResult,
  VideoParseResult,
} from "../types/api";
import type { ReactNode } from "react";
vi.mock("../lib/taskTracker", async (original) => ({
  ...(await original<object>()),
  createTaskTracker: vi.fn(() => ({ stop: vi.fn() })),
}));
const metadata = {
  platform: "demo",
  platform_video_id: "one",
  title: "测试",
  duration: 20,
  has_subtitles: false,
  subtitle_languages: [],
  download_options: [],
} as unknown as VideoParseResult;
const text = (id: string) =>
  ({
    id,
    segments: [{ start: 0, end: 10, text: id }],
    language: "zh",
    source_type: "asr",
  }) as TranscriptResult;
const ai = (id: string, transcript_id: string, result_type = "summary") =>
  ({ id, transcript_id, result_type, output_language: "zh" }) as AIResult;
function job(id: string, transcript_id: string, result_id: string): LocalTask {
  return {
    id,
    videoKey: "demo_one",
    kind: "ai_summary",
    title: "测试",
    label: "总结",
    createdAt: "2026-11-01",
    event: { status: "completed" },
    snapshot: { result_payload: { transcript_id, result_id } },
  } as LocalTask;
}
function login(id = "reader") {
  authStore.accept({
    status: "authenticated",
    access_token: "token",
    user: {
      id,
      nickname: "用户",
      role: "user",
      status: "active",
      is_subscribed: true,
    },
  });
}
beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => data.get(k) || null,
    setItem: (k: string, v: string) => data.set(k, v),
    removeItem: (k: string) => data.delete(k),
  });
  login();
  vi.spyOn(api, "transcript").mockImplementation(async (id) => text(id));
});
afterEach(() => {
  cleanup();
  authStore.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
function setup() {
  const store = createWorkspace();
  store.switchUser("reader");
  store.remember("https://example.com", metadata);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <WorkspaceProvider store={store}>{children}</WorkspaceProvider>
  );
  return { store, wrapper };
}
it("pins the reading bundle and switches transcript and summary together only on explicit acceptance", async () => {
  const { store, wrapper } = setup();
  store.tasks.push(job("old", "text-old", "sum-old"));
  vi.spyOn(api, "result").mockImplementation(async (id) =>
    ai(id, id === "sum-old" ? "text-old" : "text-new"),
  );
  const { result } = renderHook(() => useWorkspaceResults("demo_one"), {
    wrapper,
  });
  await waitFor(() => expect(result.current.summary?.id).toBe("sum-old"));
  act(() => {
    store.tasks.unshift(job("new", "text-new", "sum-new"));
    store.remember("https://example.com", metadata);
  });
  await waitFor(() => expect(result.current.hasNewResults).toBe(true));
  expect(result.current.transcript?.id).toBe("text-old");
  expect(result.current.summary?.id).toBe("sum-old");
  act(() => result.current.acceptNewResults());
  expect(result.current.transcript?.id).toBe("text-new");
  expect(result.current.summary?.id).toBe("sum-new");
  expect(result.current.hasNewResults).toBe(false);
  store.stopAll();
});
it("loads the selected result's canonical transcript and rejects maps from another time axis", async () => {
  const { store, wrapper } = setup();
  store.tasks.push(job("new", "text-new", "map-new"));
  store.remember("https://example.com", metadata, false, {
    transcriptId: "text-new",
    summaryId: "sum-old",
    mindmapId: "map-new",
  });
  vi.spyOn(api, "result").mockImplementation(async (id) =>
    ai(
      id,
      id === "sum-old" ? "text-old" : "text-new",
      id.startsWith("map") ? "mindmap" : "summary",
    ),
  );
  const { result } = renderHook(
    () => useWorkspaceResults("demo_one", null, "sum-old"),
    { wrapper },
  );
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.transcript?.id).toBe("text-old");
  expect(result.current.summary?.id).toBe("sum-old");
  expect(result.current.mindmap).toBeUndefined();
  store.stopAll();
});
it("does not re-pin while filling the first summary for an already readable transcript", async () => {
  const { store, wrapper } = setup();
  store.remember("https://example.com", metadata, false, {
    transcriptId: "text-old",
  });
  vi.spyOn(api, "result").mockResolvedValue(ai("sum-old", "text-old"));
  const { result } = renderHook(() => useWorkspaceResults("demo_one"), {
    wrapper,
  });
  await waitFor(() => expect(result.current.transcript?.id).toBe("text-old"));
  act(() =>
    store.remember("https://example.com", metadata, false, {
      transcriptId: "text-old",
      summaryId: "sum-old",
    }),
  );
  await waitFor(() => expect(result.current.summary?.id).toBe("sum-old"));
  expect(result.current.hasNewResults).toBe(false);
  store.stopAll();
});
it("aborts old identity requests and hides their results immediately after an account change", async () => {
  const { store, wrapper } = setup();
  store.tasks.push(job("old", "text-old", "sum-old"));
  let finish!: (value: AIResult) => void;
  const get = vi.spyOn(api, "result").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const { result } = renderHook(() => useWorkspaceResults("demo_one"), {
    wrapper,
  });
  await waitFor(() => expect(get).toHaveBeenCalled());
  const signal = get.mock.calls[0]![1]!;
  act(() => {
    login("other");
    store.switchUser("other");
  });
  expect(signal.aborted).toBe(true);
  await act(async () => finish(ai("sum-old", "text-old")));
  expect(result.current.transcript).toBeUndefined();
  expect(result.current.summary).toBeUndefined();
  store.stopAll();
});
