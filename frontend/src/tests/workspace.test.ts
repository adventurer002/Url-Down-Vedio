import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { api } from "../api";
import { useWorkspace } from "../stores/workspace";
import type { VideoParseResult } from "../types/api";
vi.mock("../composables/useTaskPoller", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createTaskTracker: vi.fn(() => ({ stop: vi.fn() })),
}));
beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => data.get(k) || null,
    setItem: (k: string, v: string) => data.set(k, v),
  });
  setActivePinia(createPinia());
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
const metadata: VideoParseResult = {
  platform: "demo",
  platform_video_id: "one",
  title: "测试视频",
  duration: 60,
  thumbnail: null,
  uploader: null,
  has_subtitles: true,
  subtitle_languages: ["zh"],
  download_options: [],
};
it("persists parsed video metadata so direct refresh can restore the workspace", () => {
  const store = useWorkspace(),
    key = store.remember("https://example.com/video", metadata);
  expect(key).toBe("demo_one");
  setActivePinia(createPinia());
  expect(useWorkspace().videos[0]?.metadata.title).toBe("测试视频");
});
it("prevents duplicate active jobs and retains independent job kinds", async () => {
  const store = useWorkspace();
  store.remember("https://example.com/video", metadata);
  const create = vi
    .spyOn(api, "create")
    .mockResolvedValue({ task_id: "job-one", status: "queued" });
  await store.create(store.videos[0]!, "ai_summary", "总结");
  await expect(
    store.create(store.videos[0]!, "ai_summary", "总结"),
  ).rejects.toMatchObject({ code: "TASK_ALREADY_RUNNING" });
  expect(create).toHaveBeenCalledTimes(1);
  expect(store.activeCount).toBe(1);
});
it("clears submission state when enqueue fails, allowing retry", async () => {
  const store = useWorkspace();
  store.remember("https://example.com/video", metadata);
  vi.spyOn(api, "create").mockRejectedValue(new Error("offline"));
  await expect(
    store.create(store.videos[0]!, "ai_mindmap", "导图"),
  ).rejects.toThrow("offline");
  expect(store.submitting).toEqual([]);
  expect(store.tasks).toEqual([]);
});

it("loads server history and reconstructs workspaces missing from browser storage", async () => {
  const item = {
    task_id: "server-task",
    task_type: "ai_summary",
    status: "completed",
    progress: 100,
    seq: 5,
    stage: "completed",
    error_code: null,
    updated_at: "2026-10-21T10:00:00Z",
    created_at: "2026-10-21T10:00:00Z",
    result_payload: { result_id: "result-id" },
    video_title: "服务器视频",
    download_available: false,
    video: {
      id: "video-id",
      platform: "bilibili",
      platform_video_id: "BVfixture",
      source_url: "https://example.com/video",
      title: "服务器视频",
      duration: 100,
      thumbnail: null,
      uploader: null,
      subtitle_languages: ["zh"],
    },
  } as unknown as import("../types/api").TaskHistoryItem;
  vi.spyOn(api, "history").mockResolvedValue({
    items: [item],
    total: 1,
    offset: 0,
    limit: 20,
  });
  const store = useWorkspace();
  await store.loadHistory();
  expect(store.historyTasks[0]?.snapshot?.result_payload?.result_id).toBe(
    "result-id",
  );
  expect(store.videos[0]?.partial).toBe(true);
  expect(store.videos[0]?.metadata.title).toBe("服务器视频");
  expect(store.historyTotal).toBe(1);
});

it("replaces stale downloadable IDs after a server cleanup refresh", async () => {
  const item = {
    task_id: "server-file",
    task_type: "download",
    status: "completed",
    progress: 100,
    seq: 5,
    stage: "completed",
    error_code: null,
    updated_at: "2026-10-21T10:00:00Z",
    created_at: "2026-10-21T10:00:00Z",
    result_payload: { file_id: "file-id" },
    video: null,
    video_title: null,
    download_available: true,
  } as unknown as import("../types/api").TaskHistoryItem;
  const history = vi
    .spyOn(api, "history")
    .mockResolvedValue({ items: [item], total: 1, offset: 0, limit: 20 });
  const store = useWorkspace();
  await store.loadHistory();
  expect(store.historyTasks[0]?.snapshot?.result_payload?.file_id).toBe(
    "file-id",
  );
  history.mockResolvedValue({
    items: [{ ...item, result_payload: null, download_available: false }],
    total: 1,
    offset: 0,
    limit: 20,
  });
  await store.loadHistory();
  expect(store.historyTasks[0]?.snapshot?.result_payload).toBeNull();
});
