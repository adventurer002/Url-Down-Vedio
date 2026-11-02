// @vitest-environment jsdom
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { api } from "../api";
import { ApiError } from "../api/client";
import { authStore } from "../stores/auth";
import { createUploadManager, validateFile } from "../stores/uploads";
import {
  fromUpload,
  processingInput,
  sourceLabel,
  contentKey,
} from "../lib/content";
import { createWorkspace, type LocalTask } from "../stores/workspace";
import type { UploadRecord } from "../types/uploads";
vi.mock("../lib/taskTracker", async (original) => ({
  ...(await original<object>()),
  createTaskTracker: vi.fn(() => ({ stop: vi.fn() })),
}));
const record: UploadRecord = {
  id: "upload-1",
  filename: "recording.mp3",
  expected_bytes: 4,
  size_bytes: 0,
  content_sha256: null,
  status: "pending",
  video_id: null,
  task_id: null,
  duration_seconds: null,
  media_type: null,
  error_code: null,
  created_at: "2026-11-01",
  expires_at: "2099-01-01",
};
const file = () => new File(["data"], record.filename, { type: "audio/mpeg" });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function login(id = "upload-user") {
  authStore.accept({
    status: "authenticated",
    access_token: "test",
    user: {
      id,
      nickname: "上传用户",
      role: "user",
      status: "active",
      is_subscribed: true,
    },
  });
}
function setup() {
  const tasks: LocalTask[] = [];
  const hooks = {
    notify: vi.fn(),
    record: vi.fn((r: UploadRecord) => fromUpload(r).key),
    probe: vi.fn(),
    tasks: () => tasks,
    cancelTask: vi.fn(async () => {}),
    snapshot: vi.fn(),
  };
  const manager = createUploadManager(hooks);
  manager.switchUser("upload-user");
  return { manager, hooks, tasks };
}
beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => data.get(k) || null,
    setItem: (k: string, v: string) => data.set(k, v),
    removeItem: (k: string) => data.delete(k),
  });
  login();
  vi.spyOn(api, "createUpload").mockResolvedValue(record);
  vi.spyOn(api, "completeUpload").mockResolvedValue({
    task_id: "probe-1",
    status: "queued",
  });
  vi.spyOn(api, "upload").mockResolvedValue(record);
});
afterEach(() => {
  authStore.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
it("validates supported file extensions, empty bytes and account size before HTTP", () => {
  for (const name of [
    "a.mp4",
    "a.MOV",
    "a.mkv",
    "a.webm",
    "a.mp3",
    "a.m4a",
    "A.M4A",
  ])
    expect(() => validateFile(new File(["x"], name))).not.toThrow();
  expect(() => validateFile(new File(["x"], "a.wav"))).toThrow();
  expect(() => validateFile(new File([], "a.mp3"))).toThrow();
  expect(() => validateFile(file(), 3)).toThrow();
});
it("sends raw File, separates transfer progress from ACK, and starts one probe after ACK", async () => {
  const ack = deferred<UploadRecord>();
  const send = vi.spyOn(api, "uploadContent").mockReturnValue(ack.promise);
  const { manager, hooks } = setup();
  await manager.begin(file());
  expect(send.mock.calls[0]![1]).toBeInstanceOf(File);
  expect(manager.entries[0]!.phase).toBe("sending");
  send.mock.calls[0]![3]!(4);
  expect(manager.entries[0]!.phase).toBe("confirming");
  expect(api.completeUpload).not.toHaveBeenCalled();
  expect(
    JSON.parse(localStorage.getItem("videohub.uploads.v1:upload-user")!)[0],
  ).not.toHaveProperty("file");
  ack.resolve({ ...record, status: "uploaded", size_bytes: 4 });
  await waitFor(() => expect(hooks.probe).toHaveBeenCalledOnce());
  expect(manager.entries[0]!.phase).toBe("validating");
});
it("recovers lost ACK and resumes server confirmation without retransmitting bytes", async () => {
  vi.spyOn(api, "uploadContent").mockRejectedValue(
    new ApiError("NETWORK_ERROR", "连接中断"),
  );
  vi.mocked(api.upload).mockResolvedValue({
    ...record,
    status: "uploaded",
    size_bytes: 4,
  });
  const { manager, hooks } = setup();
  await manager.begin(file());
  await waitFor(() =>
    expect(manager.entries[0]!.record.status).toBe("uploaded"),
  );
  await Promise.all([manager.resume(record.id), manager.resume(record.id)]);
  expect(api.uploadContent).toHaveBeenCalledOnce();
  expect(api.completeUpload).toHaveBeenCalledOnce();
  expect(hooks.probe).toHaveBeenCalledOnce();
});
it.each(["pending", "uploaded"] as const)(
  "reload recovers %s metadata without storing or sending File",
  async (status) => {
    localStorage.setItem(
      "videohub.uploads.v1:upload-user",
      JSON.stringify([
        {
          id: record.id,
          key: "upload_upload-1",
          record,
          phase: "sending",
          loaded: 3,
        },
      ]),
    );
    vi.mocked(api.upload).mockResolvedValue({ ...record, status });
    const send = vi.spyOn(api, "uploadContent");
    const { manager } = setup();
    expect(manager.hasFile(record.id)).toBe(false);
    await manager.recover(record.id);
    expect(send).not.toHaveBeenCalled();
    expect(manager.entries[0]!.phase).toBe(
      status === "uploaded" ? "validating" : "interrupted",
    );
  },
);
it("rejects a different reselected file and locks concurrent retries", async () => {
  const { manager } = setup();
  manager.adopt(record);
  await expect(
    manager.resume(record.id, new File(["data"], "different.mp3")),
  ).rejects.toThrow("原文件");
  const ack = deferred<UploadRecord>();
  vi.spyOn(api, "uploadContent").mockReturnValue(ack.promise);
  const first = manager.resume(record.id, file());
  await waitFor(() => expect(api.uploadContent).toHaveBeenCalledOnce());
  await manager.resume(record.id, file());
  expect(api.uploadContent).toHaveBeenCalledOnce();
  ack.resolve({ ...record, status: "uploaded" });
  await first;
});
it("aborts on identity change, revokes previews and ignores late upload ACK", async () => {
  const ack = deferred<UploadRecord>();
  const send = vi.spyOn(api, "uploadContent").mockReturnValue(ack.promise);
  vi.stubGlobal(
    "URL",
    class extends URL {
      static createObjectURL = vi.fn(() => "blob:private");
      static revokeObjectURL = vi.fn();
    },
  );
  const { manager, hooks } = setup();
  await manager.begin(file());
  expect(manager.preview(record.id)).toBe("blob:private");
  const signal = send.mock.calls[0]![2]!;
  login("other");
  manager.switchUser("other");
  expect(signal.aborted).toBe(true);
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:private");
  ack.resolve({ ...record, status: "uploaded" });
  await new Promise((r) => setTimeout(r, 0));
  expect(manager.entries).toEqual([]);
  expect(manager.hasFile(record.id)).toBe(false);
  expect(hooks.probe).not.toHaveBeenCalled();
});
it("prevents duplicate session creation while the first request is pending", async () => {
  const create = deferred<UploadRecord>();
  vi.mocked(api.createUpload).mockReturnValue(create.promise);
  vi.spyOn(api, "uploadContent").mockReturnValue(new Promise(() => {}));
  const { manager } = setup();
  const first = manager.begin(file());
  await expect(manager.begin(file())).rejects.toThrow("正在上传");
  expect(api.createUpload).toHaveBeenCalledOnce();
  create.resolve(record);
  await first;
  manager.switchUser("");
});
it("waits for server task termination before deleting a source", async () => {
  const { manager, hooks, tasks } = setup();
  manager.adopt({ ...record, status: "validating", task_id: "probe-1" });
  vi.mocked(api.upload).mockResolvedValue({
    ...record,
    status: "validating",
    task_id: "probe-1",
  });
  const task = {
    id: "probe-1",
    videoKey: "upload_upload-1",
    event: { status: "running" },
  } as LocalTask;
  tasks.push(task);
  const deleted = vi
    .spyOn(api, "deleteUpload")
    .mockResolvedValue({ ...record, status: "deleted" });
  vi.spyOn(api, "task").mockResolvedValue({
    task_id: "probe-1",
    status: "cancelled",
    seq: 2,
  } as never);
  hooks.snapshot.mockImplementation((t: LocalTask, s: LocalTask["event"]) => {
    t.event = s;
  });
  await expect(manager.remove(record.id, false)).rejects.toThrow("正在处理");
  expect(deleted).not.toHaveBeenCalled();
  await manager.remove(record.id, true);
  expect(hooks.cancelTask).toHaveBeenCalledWith(task);
  expect(task.event.status).toBe("cancelled");
  expect(deleted).toHaveBeenCalledOnce();
});
it("canonicalizes private content without null platform collisions, preserving route aliases and result refs", () => {
  const store = createWorkspace();
  store.switchUser("upload-user");
  const old = store.rememberUpload(record);
  store.findContent(old)!.results = { transcriptId: "text-1" };
  const next = store.rememberUpload({
    ...record,
    status: "ready",
    video_id: "video-1",
  });
  expect(next).toBe("content_video-1");
  expect(store.findContent(old)).toBe(store.findContent(next));
  expect(store.findContent(next)!.results!.transcriptId).toBe("text-1");
  expect(fromUpload({ ...record, id: "second" }).key).not.toBe(old);
  expect(
    contentKey({
      id: "private-2",
      platform: null,
      platform_video_id: null,
    } as never),
  ).toBe("content_private-2");
  store.stopAll();
});
it("uses only transcript_id for AI after deleting or expiring source files", () => {
  const source = fromUpload({ ...record, status: "deleted" });
  expect(processingInput(source, "ai_summary", "text-1")).toEqual({
    transcript_id: "text-1",
  });
  expect(processingInput(source, "ai_mindmap", "text-1")).toEqual({
    transcript_id: "text-1",
  });
  expect(() => processingInput(source, "transcription")).toThrow(
    "原文件已到期",
  );
});

it.each(["recording.m4a", "RECORDING.M4A"])(
  "recognizes %s as M4A audio before probing",
  (filename) => {
    const audio = fromUpload({ ...record, filename });
    expect(audio.source?.mediaType).toBe("audio");
    expect(sourceLabel(audio)).toBe("M4A 音频");
    expect(processingInput(audio, "ai_summary", "text-1")).toEqual({
      transcript_id: "text-1",
    });
  },
);
