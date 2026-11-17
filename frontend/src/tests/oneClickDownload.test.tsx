// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { api } from "../api";
import { ApiError, client } from "../api/client";
import { authStore } from "../stores/auth";
import { createWorkspace } from "../stores/workspace";
import { createTaskTracker } from "../lib/taskTracker";
import { billingStore } from "../stores/billing";
import type { TaskSnapshot, VideoParseResult } from "../types/api";

vi.mock("../lib/taskTracker", async (original) => ({
  ...(await original<object>()),
  createTaskTracker: vi.fn(() => ({ stop: vi.fn() })),
}));

beforeEach(() => {
  const saved = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => saved.get(key) ?? null,
    setItem: (key: string, value: string) => saved.set(key, value),
    removeItem: (key: string) => saved.delete(key),
  });
  authStore.accept({
    status: "authenticated",
    access_token: "download-token",
    user: {
      id: "download-user",
      nickname: "下载用户",
      role: "user",
      status: "active",
      is_subscribed: true,
    },
  });
  vi.spyOn(api, "create").mockResolvedValue({
    task_id: "download-1",
    status: "queued",
  });
  vi.spyOn(client, "post").mockResolvedValue({
    data: { access_token: "scoped-ticket" },
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  vi.spyOn(billingStore, "refresh").mockResolvedValue();
});
afterEach(() => {
  authStore.clear();
  vi.restoreAllMocks();
  vi.mocked(createTaskTracker).mockClear();
  vi.unstubAllGlobals();
});

async function queued(kind: "download" | "audio_extraction" = "download") {
  const store = createWorkspace();
  store.switchUser("download-user");
  store.remember("https://example.com/video", {
    platform: "demo",
    platform_video_id: "one",
    title: "下载测试",
    duration: 60,
    thumbnail: null,
    uploader: null,
    has_subtitles: false,
    subtitle_languages: [],
    download_options: [],
  } satisfies VideoParseResult);
  const task = await store.create(store.videos[0]!, kind, "下载测试");
  const callbacks = vi.mocked(createTaskTracker).mock.calls.at(-1)![1];
  return { store, task, callbacks };
}
function completed(result: Record<string, string>): TaskSnapshot {
  return {
    task_id: "download-1",
    status: "completed",
    stage: "completed",
    seq: 2,
    progress: 100,
    error_code: null,
    updated_at: "2026-11-17",
    result_payload: result,
  } as TaskSnapshot;
}

it.each(["download", "audio_extraction"] as const)(
  "downloads %s once after its snapshot arrives without a visible card",
  async (kind) => {
    const { store, task, callbacks } = await queued(kind);
    store.downloadWhenReady(task);
    const snapshot = completed({ file_id: "ready-file" });
    callbacks.onEvent(snapshot);
    expect(client.post).not.toHaveBeenCalled();
    callbacks.onSnapshot(snapshot);
    callbacks.onSnapshot(snapshot);
    await waitFor(() =>
      expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(1),
    );
    expect(client.post).toHaveBeenCalledWith(
      "/files/ready-file/download/access",
      undefined,
      expect.any(Object),
    );
    expect(store.browserDownloads[task.id].handedOff).toBe(true);
    store.stopAll();
  },
);

it("immediately hands a prepared delivery to the browser using its scoped ticket", async () => {
  const { store, task, callbacks } = await queued();
  callbacks.onEvent(completed({ delivery_id: "prepared-session" }));
  callbacks.onSnapshot(completed({ delivery_id: "prepared-session" }));
  expect(client.post).not.toHaveBeenCalled();
  store.downloadWhenReady(task);
  await waitFor(() =>
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce(),
  );
  expect(client.post).toHaveBeenCalledWith(
    "/deliveries/prepared-session/download/access",
    undefined,
    expect.any(Object),
  );
  store.stopAll();
});

it.each(["cancelled", "failed"] as const)(
  "keeps a %s task from starting a download",
  async (status) => {
    const { store, task, callbacks } = await queued();
    store.downloadWhenReady(task);
    const snapshot = { ...completed({}), status };
    callbacks.onEvent(snapshot);
    callbacks.onSnapshot(snapshot);
    callbacks.onEvent(completed({ file_id: "late-file" }));
    callbacks.onSnapshot(completed({ file_id: "late-file" }));
    expect(client.post).not.toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
    store.stopAll();
  },
);

it("aborts a pending ticket on account change and ignores its late response", async () => {
  const { store, task, callbacks } = await queued();
  let resolve!: (value: { data: { access_token: string } }) => void;
  vi.mocked(client.post).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  store.downloadWhenReady(task);
  callbacks.onEvent(completed({ file_id: "owned-file" }));
  callbacks.onSnapshot(completed({ file_id: "owned-file" }));
  const signal = vi.mocked(client.post).mock.calls[0]![2]!
    .signal as AbortSignal;
  store.switchUser("another-user");
  expect(signal.aborted).toBe(true);
  resolve({ data: { access_token: "late-ticket" } });
  await Promise.resolve();
  await Promise.resolve();
  expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  expect(store.browserDownloads).toEqual({});
  store.stopAll();
});

it("reports an automatic ticket failure and allows an explicit retry", async () => {
  const { store, task, callbacks } = await queued();
  vi.mocked(client.post).mockRejectedValueOnce(
    new ApiError("NETWORK_ERROR", "下载连接暂时不可用"),
  );
  store.downloadWhenReady(task);
  callbacks.onEvent(completed({ file_id: "retry-file" }));
  callbacks.onSnapshot(completed({ file_id: "retry-file" }));
  await waitFor(() =>
    expect(store.browserDownloads[task.id].error?.code).toBe("NETWORK_ERROR"),
  );
  expect(client.post).toHaveBeenCalledTimes(1);
  await store.saveDownload(task);
  expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce();
  expect(store.browserDownloads[task.id]).toEqual({
    pending: false,
    handedOff: true,
    error: null,
  });
  store.stopAll();
});

it("restores completed history without starting old downloads", async () => {
  const { store, callbacks } = await queued();
  callbacks.onEvent(completed({ file_id: "historical-file" }));
  callbacks.onSnapshot(completed({ file_id: "historical-file" }));
  store.stopAll();
  const restored = createWorkspace();
  restored.switchUser("download-user");
  expect(restored.tasks[0]?.snapshot?.result_payload?.file_id).toBe(
    "historical-file",
  );
  expect(restored.browserDownloads).toEqual({});
  expect(client.post).not.toHaveBeenCalled();
  restored.stopAll();
});
