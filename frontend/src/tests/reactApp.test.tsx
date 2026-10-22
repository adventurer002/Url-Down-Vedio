// @vitest-environment jsdom
import { StrictMode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../App";
import { api } from "../api";
import { ApiError } from "../api/client";
import { WorkspaceProvider } from "../hooks/useWorkspace";
import { createWorkspace } from "../stores/workspace";
import type {
  SummaryResult,
  TaskSnapshot,
  TranscriptResult,
  VideoParseResult,
} from "../types/api";

const metadata: VideoParseResult = {
  platform: "demo",
  platform_video_id: "one",
  title: "React 测试视频",
  duration: 93,
  thumbnail: null,
  uploader: "测试作者",
  has_subtitles: true,
  subtitle_languages: ["zh"],
  download_options: [
    {
      id: "video_720_mp4",
      label: "720P",
      height: 720,
      container: "mp4",
      estimated_size_bytes: 1024,
    },
  ],
};
const transcript = {
  id: "transcript-1",
  language: "zh",
  source_type: "subtitle",
  segments: [{ start: 0, end: 10, text: "测试字幕内容" }],
} as TranscriptResult;
const summary = {
  id: "summary-1",
  result_type: "summary",
  transcript_id: "transcript-1",
  llm_provider: "heuristic",
  input_tokens: null,
  duration_ms: 100,
  content_json: {
    source_basis: "subtitle",
    overview: "测试视频概述",
    key_points: ["关键要点"],
    chapters: [{ start_seconds: 93, title: "章节标题", summary: "章节摘要" }],
  },
} as SummaryResult;
function snapshot(status: TaskSnapshot["status"], seq = 3): TaskSnapshot {
  return {
    task_id: "job-1",
    status,
    seq,
    progress: status === "completed" ? 100 : 30,
    stage: status,
    error_code: null,
    updated_at: "2026-10-22",
    cancel_requested: false,
    result_payload: status === "completed" ? { file_id: "file-1" } : null,
  } as TaskSnapshot;
}
class FakeSource {
  static instances: FakeSource[] = [];
  onerror: (() => void) | null = null;
  listeners = new Map<string, (event: MessageEvent) => void>();
  close = vi.fn();
  constructor() {
    FakeSource.instances.push(this);
  }
  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, listener);
  }
  emit(value: TaskSnapshot) {
    this.listeners.get("progress")?.({
      data: JSON.stringify(value),
    } as MessageEvent);
  }
}
beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => data.set(key, value),
    removeItem: (key: string) => data.delete(key),
    clear: () => data.clear(),
  });
  FakeSource.instances = [];
  vi.stubGlobal("EventSource", FakeSource);
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value() {
      this.setAttribute("open", "");
    },
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value() {
      this.removeAttribute("open");
    },
  });
  vi.spyOn(api, "ready").mockResolvedValue({
    status: "ready",
    database: "ready",
    redis: "ready",
  });
  vi.spyOn(api, "history").mockResolvedValue({
    items: [],
    total: 0,
    offset: 0,
    limit: 20,
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
function mount(path = "/", store = createWorkspace()) {
  const view = render(
    <StrictMode>
      <MemoryRouter initialEntries={[path]}>
        <WorkspaceProvider store={store}>
          <App />
        </WorkspaceProvider>
      </MemoryRouter>
    </StrictMode>,
  );
  return { ...view, store };
}
function savedStore() {
  const store = createWorkspace();
  store.remember("https://example.com/video", metadata);
  return store;
}

it("parses a URL, navigates to the React workspace and retains metadata after refresh", async () => {
  vi.spyOn(api, "parse").mockResolvedValue(metadata);
  mount();
  fireEvent.change(screen.getByLabelText("视频链接"), {
    target: { value: "https://example.com/video" },
  });
  fireEvent.click(screen.getByRole("button", { name: "开始解析" }));
  expect(
    await screen.findByRole("heading", { name: metadata.title }),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: /720P MP4/ })).toBeTruthy();
  cleanup();
  mount("/video/demo_one");
  expect(
    await screen.findByRole("heading", { name: metadata.title }),
  ).toBeTruthy();
});

it("preserves the input and request ID on API errors and allows retry", async () => {
  vi.spyOn(api, "parse")
    .mockRejectedValueOnce(
      new ApiError("SSRF_BLOCKED", "不能访问私网", "req_test"),
    )
    .mockResolvedValue(metadata);
  mount();
  const input = screen.getByLabelText("视频链接") as HTMLInputElement;
  fireEvent.change(input, { target: { value: "http://127.0.0.1/private" } });
  fireEvent.click(screen.getByRole("button", { name: "开始解析" }));
  expect(await screen.findByRole("alert")).toHaveProperty(
    "textContent",
    expect.stringContaining("req_test"),
  );
  expect(input.value).toBe("http://127.0.0.1/private");
  fireEvent.click(screen.getByRole("button", { name: /保留链接/ }));
  expect(
    await screen.findByRole("heading", { name: metadata.title }),
  ).toBeTruthy();
});

it("reacts to SSE progress, fetches the completed result and exposes the controlled download", async () => {
  vi.spyOn(api, "create").mockResolvedValue({
    task_id: "job-1",
    status: "queued",
  });
  vi.spyOn(api, "task").mockResolvedValue(snapshot("completed"));
  mount("/video/demo_one", savedStore());
  fireEvent.click(await screen.findByRole("button", { name: /720P MP4/ }));
  await waitFor(() => expect(FakeSource.instances.length).toBe(1));
  act(() => FakeSource.instances[0]!.emit(snapshot("running", 2)));
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe(
    "30",
  );
  act(() => FakeSource.instances[0]!.emit(snapshot("completed")));
  expect(
    await screen.findByRole("link", { name: "保存到本地" }),
  ).toHaveProperty(
    "href",
    "http://localhost:8000/api/v1/files/file-1/download",
  );
  expect(FakeSource.instances[0]!.close).toHaveBeenCalled();
});

it("cancels an active task through the store and updates the React task card", async () => {
  vi.spyOn(api, "create").mockResolvedValue({
    task_id: "job-1",
    status: "queued",
  });
  vi.spyOn(api, "cancel").mockResolvedValue(snapshot("cancelled"));
  mount("/video/demo_one", savedStore());
  fireEvent.click(await screen.findByRole("button", { name: /720P MP4/ }));
  fireEvent.click(await screen.findByRole("button", { name: "取消任务" }));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "取消任务" })).toBeNull(),
  );
  expect(screen.getAllByText(/已取消/).length).toBeGreaterThan(0);
  expect(api.cancel).toHaveBeenCalledWith("job-1");
});

it("restores a summary and transcript and switches mobile tabs", async () => {
  const store = savedStore();
  vi.spyOn(api, "create").mockResolvedValue({
    task_id: "job-1",
    status: "queued",
  });
  vi.spyOn(api, "result").mockResolvedValue(summary);
  vi.spyOn(api, "transcript").mockResolvedValue(transcript);
  await store.create(store.videos[0]!, "ai_summary", "总结");
  store.stopAll();
  store.tasks[0]!.event = snapshot("completed");
  store.tasks[0]!.snapshot = {
    ...snapshot("completed"),
    result_payload: { result_id: "summary-1" },
  };
  mount("/video/demo_one", store);
  expect(await screen.findByText("测试视频概述")).toBeTruthy();
  expect(screen.getByText("[01:33]")).toBeTruthy();
  expect(screen.queryByText("测试字幕内容")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /查看原始文稿/ }));
  expect(screen.getByText("测试字幕内容")).toBeTruthy();
  const tab = screen.getByRole("button", { name: "视频与下载" });
  fireEvent.click(tab);
  expect(tab.getAttribute("aria-pressed")).toBe("true");
  expect(document.querySelector(".media-panel.mobile-active")).toBeTruthy();
});

it("opens task history, cleans expired files and returns focus on Escape", async () => {
  vi.spyOn(api, "cleanup").mockResolvedValue({
    files: 1,
    orphan_files: 0,
    temporary_directories: 1,
    freed_bytes: 2048,
    used_bytes: 0,
  });
  mount();
  const trigger = screen.getByRole("button", { name: "任务历史" });
  trigger.focus();
  fireEvent.click(trigger);
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(screen.getByRole("button", { name: "清理过期缓存文件" }));
  expect(await screen.findByText(/已释放 2.0 KB/)).toBeTruthy();
  fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
  expect(dialog.hasAttribute("open")).toBe(false);
  expect(document.activeElement).toBe(trigger);
});

it("Strict Mode remounts close old SSE sources and leave only the live subscription", async () => {
  const store = savedStore();
  vi.spyOn(api, "create").mockResolvedValue({
    task_id: "job-1",
    status: "queued",
  });
  await store.create(store.videos[0]!, "download", "下载", "video_720_mp4");
  store.stopAll();
  FakeSource.instances = [];
  const view = mount("/video/demo_one", store);
  await screen.findByRole("progressbar");
  expect(FakeSource.instances.length).toBe(2);
  expect(FakeSource.instances[0]!.close).toHaveBeenCalled();
  expect(FakeSource.instances[1]!.close).not.toHaveBeenCalled();
  view.unmount();
  expect(FakeSource.instances[1]!.close).toHaveBeenCalled();
});

it("retries a failed result request and still loads its transcript", async () => {
  const store = savedStore();
  store.tasks.push({
    id: "job-1",
    videoKey: "demo_one",
    title: metadata.title,
    kind: "ai_summary",
    label: "总结",
    createdAt: "2026-10-22",
    event: snapshot("completed"),
    snapshot: {
      ...snapshot("completed"),
      result_payload: { result_id: "summary-1" },
    },
  });
  vi.spyOn(api, "result")
    .mockRejectedValueOnce(new ApiError("TEMPORARY_FAILURE", "结果暂不可用"))
    .mockRejectedValueOnce(new ApiError("TEMPORARY_FAILURE", "结果暂不可用"))
    .mockResolvedValue(summary);
  vi.spyOn(api, "transcript").mockResolvedValue(transcript);
  mount("/video/demo_one", store);
  await screen.findByRole("button", { name: "重新加载结果" });
  // Strict Mode may cancel its first effect before or after the request starts.
  vi.mocked(api.result).mockResolvedValue(summary);
  fireEvent.click(screen.getByRole("button", { name: "重新加载结果" }));
  expect(await screen.findByText("测试视频概述")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "重新加载结果" })).toBeNull();
});

it("aborts pending result requests when leaving a workspace and ignores late responses", async () => {
  const store = savedStore();
  store.tasks.push({
    id: "job-1",
    videoKey: "demo_one",
    title: metadata.title,
    kind: "ai_summary",
    label: "总结",
    createdAt: "2026-10-22",
    event: snapshot("completed"),
    snapshot: {
      ...snapshot("completed"),
      result_payload: { result_id: "summary-1" },
    },
  });
  let resolveResult!: (value: SummaryResult) => void;
  const pending = new Promise<SummaryResult>((resolve) => {
    resolveResult = resolve;
  });
  const result = vi.spyOn(api, "result").mockReturnValue(pending);
  const getTranscript = vi
    .spyOn(api, "transcript")
    .mockResolvedValue(transcript);
  mount("/video/demo_one", store);
  await screen.findByRole("heading", { name: metadata.title });
  await waitFor(() => expect(result).toHaveBeenCalled());
  const signal = result.mock.calls.at(-1)![1]!;
  fireEvent.click(screen.getByRole("link", { name: "VideoHub 首页" }));
  expect(signal.aborted).toBe(true);
  await act(async () => {
    resolveResult(summary);
    await pending;
  });
  expect(screen.queryByText("测试视频概述")).toBeNull();
  expect(getTranscript).not.toHaveBeenCalled();
});
