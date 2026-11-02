import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { createWorkspace } from "../stores/workspace";
import type { VideoParseResult } from "../types/api";
vi.mock("../lib/taskTracker", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createTaskTracker: vi.fn(() => ({ stop: vi.fn() })),
}));
beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => data.get(k) || null,
    setItem: (k: string, v: string) => data.set(k, v),
  });
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
it("restores each user's videos and tasks without overwriting their saved cache", async () => {
  vi.spyOn(api, "create").mockResolvedValue({
    task_id: "owned-job",
    status: "queued",
  });
  const store = createWorkspace();
  store.switchUser("user-a");
  store.remember("https://example.com/video", metadata, false, {
    transcriptId: "saved-transcript",
  });
  await store.create(store.videos[0]!, "ai_summary", "总结");
  store.stopAll();
  const reloaded = createWorkspace();
  reloaded.switchUser("user-a");
  expect(reloaded.videos[0]?.results?.transcriptId).toBe("saved-transcript");
  expect(reloaded.tasks[0]?.id).toBe("owned-job");
  reloaded.switchUser("user-b");
  expect(reloaded.videos).toEqual([]);
  expect(reloaded.tasks).toEqual([]);
  reloaded.switchUser("user-a");
  expect(reloaded.videos[0]?.metadata.title).toBe(metadata.title);
  expect(reloaded.tasks[0]?.id).toBe("owned-job");
  reloaded.stopAll();
});

it("throttles progress storage, immediately saves terminal results and flushes before switching users", async () => {
  vi.useFakeTimers();
  const { createTaskTracker } = await import("../lib/taskTracker");
  const store = createWorkspace();
  store.switchUser("user-a");
  store.remember("https://example.com/video", metadata);
  vi.spyOn(api, "create").mockResolvedValue({
    task_id: "progress-job",
    status: "queued",
  });
  await store.create(store.videos[0]!, "download", "下载");
  const callbacks = vi.mocked(createTaskTracker).mock.calls.at(-1)![1];
  const event = {
    task_id: "progress-job",
    status: "running" as const,
    progress: 1,
    seq: 1,
    stage: "downloading",
    error_code: null,
    updated_at: "2026-10-28",
  };
  callbacks.onEvent(event);
  const writes = vi.spyOn(localStorage, "setItem");
  for (let seq = 2; seq <= 8; seq++)
    callbacks.onEvent({ ...event, seq, progress: seq });
  expect(store.tasks[0]?.event.progress).toBe(8);
  expect(writes).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1000);
  expect(writes).toHaveBeenCalledTimes(2);
  callbacks.onEvent({ ...event, seq: 9, progress: 9 });
  store.switchUser("user-b");
  expect(
    JSON.parse(localStorage.getItem("videohub.tasks.v1:user-a")!)[0].event
      .progress,
  ).toBe(9);
  // A late callback from the old identity must not write to the new account.
  callbacks.onEvent({ ...event, seq: 10, progress: 10 });
  await vi.advanceTimersByTimeAsync(1000);
  expect(localStorage.getItem("videohub.tasks.v1:user-b")).toBeNull();
  store.switchUser("user-a");
  const current = vi.mocked(createTaskTracker).mock.calls.at(-1)![1];
  writes.mockClear();
  current.onEvent({
    ...event,
    status: "completed",
    stage: "completed",
    progress: 100,
    seq: 11,
  });
  expect(writes).toHaveBeenCalledTimes(2);
  expect(
    JSON.parse(localStorage.getItem("videohub.tasks.v1:user-a")!)[0].event
      .status,
  ).toBe("completed");
  store.stopAll();
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
  const store = createWorkspace(),
    key = store.remember("https://example.com/video", metadata);
  expect(key).toBe("demo_one");
  expect(createWorkspace().videos[0]?.metadata.title).toBe("测试视频");
});
it("prevents duplicate active jobs and retains independent job kinds", async () => {
  const store = createWorkspace();
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
  const store = createWorkspace();
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
  const store = createWorkspace();
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
  const store = createWorkspace();
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

it("does not restart subscriptions when an in-flight history request finishes after cleanup", async () => {
  const { createTaskTracker } = await import("../lib/taskTracker");
  vi.mocked(createTaskTracker).mockClear();
  let resolveHistory!: (page: import("../types/api").TaskHistoryPage) => void;
  vi.spyOn(api, "history").mockImplementation(
    () =>
      new Promise((resolve) => {
        resolveHistory = resolve;
      }),
  );
  const store = createWorkspace();
  const pending = store.loadHistory();
  store.stopAll();
  resolveHistory({
    items: [
      {
        task_id: "late-task",
        task_type: "download",
        status: "running",
        progress: 20,
        seq: 2,
        stage: "downloading",
        error_code: null,
        updated_at: "2026-10-22",
        created_at: "2026-10-22",
        video: null,
      },
    ] as import("../types/api").TaskHistoryItem[],
    total: 1,
    offset: 0,
    limit: 20,
  });
  await pending;
  expect(createTaskTracker).not.toHaveBeenCalled();
  store.restore();
  expect(createTaskTracker).toHaveBeenCalledTimes(1);
});

function historyItem(id: string, status = "completed") {
  return {
    task_id: id,
    task_type: "download",
    status,
    progress: status === "completed" ? 100 : 20,
    seq: 1,
    stage: status,
    error_code: null,
    created_at: "2026-10-23T10:00:00Z",
    video: null,
    video_title: "历史任务",
    download_available: false,
  } as unknown as import("../types/api").TaskHistoryItem;
}

it("deletes history persistently without removing or cancelling active work", async () => {
  vi.spyOn(api, "history").mockResolvedValue({
    items: [historyItem("active", "running"), historyItem("done")],
    total: 2,
    offset: 0,
    limit: 20,
  });
  vi.spyOn(api, "deleteHistory").mockResolvedValue({ deleted_count: 1 });
  vi.spyOn(api, "clearHistory").mockResolvedValue({ deleted_count: 1 });
  const cancel = vi.spyOn(api, "cancel");
  const store = createWorkspace();
  await store.loadHistory();
  await store.deleteHistory("active");
  expect(store.historyTasks.map((task) => task.id)).toEqual(["done"]);
  expect(store.tasks.find((task) => task.id === "active")?.event.status).toBe(
    "running",
  );
  expect(store.activeCount).toBe(1);
  await store.deleteHistory();
  expect(store.historyTasks).toEqual([]);
  expect(store.historyTotal).toBe(0);
  expect(store.tasks).toHaveLength(2);
  expect(createWorkspace().historyTasks).toEqual([]);
  expect(cancel).not.toHaveBeenCalled();
});

it("ignores an older history response after deletion and retains records on deletion failure", async () => {
  const item = historyItem("done");
  const page = { items: [item], total: 1, offset: 0, limit: 20 };
  const history = vi.spyOn(api, "history").mockResolvedValue(page);
  const remove = vi
    .spyOn(api, "deleteHistory")
    .mockRejectedValueOnce(new Error("offline"));
  const store = createWorkspace();
  await store.loadHistory();
  await expect(store.deleteHistory("done")).rejects.toThrow("offline");
  expect(store.historyTasks).toHaveLength(1);
  expect(store.historyMutating).toBe(false);
  let resolve!: (value: typeof page) => void;
  history.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const loading = store.loadHistory();
  remove.mockResolvedValueOnce({ deleted_count: 1 });
  await store.deleteHistory("done");
  resolve(page);
  await loading;
  expect(store.historyTasks).toEqual([]);
  expect(store.historyTotal).toBe(0);
});
