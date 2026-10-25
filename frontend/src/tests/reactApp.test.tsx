import { authApi, authStore } from "../stores/auth";
// @vitest-environment jsdom
import { StrictMode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../App";
import { allowances, quote } from "./billingFixtures";
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
    conclusions: ["测试核心结论"],
    audience: ["后端开发者"],
    action_steps: ["检查事务边界"],
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
  authStore.clear();
  authStore.close();
  vi.spyOn(authStore, "restore").mockResolvedValue();
  // Existing workspace tests isolate their behavior from the login gate.
  vi.spyOn(authStore, "hasAccess").mockReturnValue(true);
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
  vi.spyOn(api, "allowances").mockResolvedValue(allowances);
  vi.spyOn(api, "quote").mockResolvedValue(quote);
  vi.spyOn(api, "confirmQuote").mockResolvedValue({
    task_id: "job-1",
    status: "queued",
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
  expect(screen.getByRole("radio", { name: /720P/ })).toBeTruthy();
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
  await screen.findByRole("radio", { name: /720P/ });
  fireEvent.click(screen.getByRole("button", { name: "下载视频" }));
  expect(api.confirmQuote).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole("button", { name: "确认并开始" }));
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
  await screen.findByRole("radio", { name: /720P/ });
  fireEvent.click(screen.getByRole("button", { name: "下载视频" }));
  fireEvent.click(await screen.findByRole("button", { name: "确认并开始" }));
  fireEvent.click(await screen.findByRole("button", { name: "取消任务" }));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "取消任务" })).toBeNull(),
  );
  expect(screen.getAllByText(/已取消/).length).toBeGreaterThan(0);
  expect(api.cancel).toHaveBeenCalledWith("job-1");
});

it("restores a summary and transcript and switches workspace tabs", async () => {
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
  fireEvent.click(await screen.findByRole("tab", { name: "文稿与总结" }));
  expect(await screen.findByText("测试视频概述")).toBeTruthy();
  expect(screen.getByText("[01:33]")).toBeTruthy();
  let finishCopy!: () => void;
  const writeText = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        finishCopy = resolve;
      }),
  );
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  fireEvent.click(screen.getByRole("button", { name: "复制总结" }));
  expect(screen.queryByText("复制成功")).toBeNull();
  await act(async () => {
    finishCopy();
  });
  expect(await screen.findByText("复制成功")).toBeTruthy();
  expect(writeText).toHaveBeenCalledWith(
    expect.stringContaining("测试视频概述"),
  );
  expect(writeText).toHaveBeenCalledWith(
    expect.stringContaining("检查事务边界"),
  );
  expect(screen.getByRole("heading", { name: "核心结论" })).toBeTruthy();
  writeText.mockRejectedValueOnce(new Error("permission denied"));
  fireEvent.click(screen.getByRole("button", { name: "复制总结" }));
  expect(await screen.findByText("复制失败，请重试")).toBeTruthy();
  expect(screen.queryByText("复制成功")).toBeNull();
  expect(screen.queryByText("测试字幕内容")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /查看原始文稿/ }));
  expect(screen.getByText("测试字幕内容")).toBeTruthy();
  const tab = screen.getByRole("tab", { name: "视频下载" });
  fireEvent.click(tab);
  expect(tab.getAttribute("aria-selected")).toBe("true");
  expect(document.querySelector(".media-panel")?.hasAttribute("hidden")).toBe(
    false,
  );
});

it("opens focused task history without storage controls and returns focus on Escape", async () => {
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
  expect(screen.queryByRole("button", { name: "清理过期缓存文件" })).toBeNull();
  expect(api.cleanup).not.toHaveBeenCalled();
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

it("prompts and focuses an empty video link without calling the parse API", async () => {
  const parse = vi.spyOn(api, "parse");
  mount();
  fireEvent.click(screen.getByRole("button", { name: "开始解析" }));
  expect(await screen.findByRole("alert")).toHaveProperty(
    "textContent",
    expect.stringContaining("请先粘贴视频链接"),
  );
  expect(document.activeElement).toBe(screen.getByLabelText("视频链接"));
  expect(parse).not.toHaveBeenCalled();
});

it("moves cache cleanup to settings and removes infrastructure status from the homepage", async () => {
  vi.spyOn(api, "cleanup").mockResolvedValue({
    files: 1,
    orphan_files: 0,
    temporary_directories: 1,
    freed_bytes: 2048,
    used_bytes: 0,
  });
  const home = mount();
  expect(screen.queryByText("PostgreSQL")).toBeNull();
  expect(screen.queryByRole("contentinfo")).toBeNull();
  expect(api.ready).not.toHaveBeenCalled();
  home.unmount();
  mount("/settings");
  const clean = await screen.findByRole("button", { name: "清理过期缓存文件" });
  fireEvent.click(clean);
  expect(await screen.findByText(/已释放 2.0 KB/)).toBeTruthy();
});

it("groups account actions behind an avatar and closes the menu on Escape", async () => {
  authStore.accept({
    status: "authenticated",
    access_token: "test-token",
    user: {
      id: "test-user",
      nickname: "微信用户_1234",
      is_subscribed: true,
      role: "user",
      status: "active",
    },
  });
  mount();
  const trigger = screen.getByRole("button", { name: "账户菜单" });
  expect(screen.queryByText("微信用户_1234")).toBeNull();
  expect(screen.queryByRole("button", { name: "退出登录" })).toBeNull();
  fireEvent.click(trigger);
  expect(screen.getByText("微信用户_1234")).toBeTruthy();
  expect(screen.getByText("已关注")).toBeTruthy();
  expect(screen.getByRole("button", { name: "退出登录" })).toBeTruthy();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(trigger);
});

it("keeps service health in the administrator page", async () => {
  authStore.accept({
    status: "authenticated",
    access_token: "admin-token",
    user: {
      id: "admin-user",
      nickname: "管理员",
      is_subscribed: true,
      role: "admin",
      status: "active",
    },
  });
  mount("/admin");
  expect(await screen.findByRole("heading", { name: "服务状态" })).toBeTruthy();
  expect(await screen.findByText("PostgreSQL")).toBeTruthy();
  await waitFor(() => expect(api.ready).toHaveBeenCalled());
});

it("does not poll service health for non-admin visitors", async () => {
  mount("/admin");
  expect(
    await screen.findByText("此页面仅供管理员查看服务运行状态。"),
  ).toBeTruthy();
  expect(api.ready).not.toHaveBeenCalled();
});

it("deletes a history item and clears the remaining records without cancelling jobs", async () => {
  let items = ["单条记录", "剩余记录"].map((title, index) => ({
    ...snapshot("completed"),
    task_id: `history-${index}`,
    task_type: "download",
    created_at: "2026-10-23T10:00:00Z",
    video: null,
    video_title: title,
    download_available: false,
    result_payload: null,
  })) as unknown as import("../types/api").TaskHistoryItem[];
  vi.mocked(api.history).mockImplementation(async () => ({
    items: [...items],
    total: items.length,
    offset: 0,
    limit: 20,
  }));
  const remove = vi
    .spyOn(api, "deleteHistory")
    .mockImplementation(async (id) => {
      items = items.filter((item) => item.task_id !== id);
      return { deleted_count: 1 };
    });
  const clear = vi.spyOn(api, "clearHistory").mockImplementation(async () => {
    const count = items.length;
    items = [];
    return { deleted_count: count };
  });
  const cancel = vi.spyOn(api, "cancel");
  mount();
  fireEvent.click(screen.getByRole("button", { name: "任务历史" }));
  const dialog = await screen.findByRole("dialog");
  await within(dialog).findByText("单条记录");
  fireEvent.click(
    within(dialog).getByRole("button", { name: "删除任务记录：单条记录" }),
  );
  await waitFor(() =>
    expect(within(dialog).queryByText("单条记录")).toBeNull(),
  );
  expect(remove).toHaveBeenCalledWith("history-0");
  fireEvent.click(within(dialog).getByRole("button", { name: "一键清除" }));
  expect(await within(dialog).findByText("一切从一个链接开始")).toBeTruthy();
  expect(clear).toHaveBeenCalledOnce();
  expect(cancel).not.toHaveBeenCalled();
});

it.each(["input", "parse", "history", "login"])(
  "opens the login card from %s without sending business requests",
  async (entry) => {
    vi.mocked(authStore.hasAccess).mockRestore();
    vi.spyOn(authApi, "create").mockResolvedValue({
      scene_id: "login-gate",
      mode: "verify_code",
      qr_url: "/wechat_qr.png",
      verify_code: "123456",
      expires_in: 300,
    });
    const parse = vi.spyOn(api, "parse");
    mount();
    if (entry === "input") fireEvent.click(screen.getByLabelText("视频链接"));
    if (entry === "parse")
      fireEvent.click(screen.getByRole("button", { name: "开始解析" }));
    if (entry === "history")
      fireEvent.click(screen.getByRole("button", { name: "任务历史" }));
    if (entry === "login")
      fireEvent.click(screen.getByRole("button", { name: "登录" }));
    const modal = await screen.findByRole("dialog", {
      name: "关注，即刻开始。",
    });
    expect(await within(modal).findByLabelText("六位登录口令")).toHaveProperty(
      "textContent",
      "123456",
    );
    expect(parse).not.toHaveBeenCalled();
    expect(api.history).not.toHaveBeenCalled();
    expect(screen.queryByText("请先粘贴视频链接")).toBeNull();
    fireEvent.click(
      within(modal).getByRole("button", { name: "关闭登录弹窗" }),
    );
    expect(
      screen.queryByRole("dialog", { name: "关注，即刻开始。" }),
    ).toBeNull();
  },
);

it("preserves workspace access for an active unsubscribed account", async () => {
  vi.mocked(authStore.hasAccess).mockRestore();
  authStore.accept({
    status: "authenticated",
    access_token: "unsubscribed-token",
    user: {
      id: "unsubscribed",
      nickname: "用户",
      is_subscribed: false,
      role: "user",
      status: "active",
    },
  });
  vi.spyOn(authApi, "create").mockResolvedValue({
    scene_id: "login-gate",
    mode: "verify_code",
    qr_url: "/wechat_qr.png",
    verify_code: "123456",
    expires_in: 300,
  });
  const parse = vi.spyOn(api, "parse");
  mount();
  const input = screen.getByLabelText("视频链接");
  expect((input as HTMLInputElement).readOnly).toBe(false);
  expect(screen.queryByRole("dialog", { name: "关注，即刻开始。" })).toBeNull();
  expect(parse).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "账户菜单" })).toBeTruthy();
});

it("lets a subscribed signed-in user paste and parse normally", async () => {
  vi.mocked(authStore.hasAccess).mockRestore();
  authStore.accept({
    status: "authenticated",
    access_token: "signed-in-token",
    user: {
      id: "signed-in",
      nickname: "已登录用户",
      is_subscribed: true,
      role: "user",
      status: "active",
    },
  });
  const parse = vi.spyOn(api, "parse").mockResolvedValue(metadata);
  mount();
  const input = screen.getByLabelText("视频链接");
  fireEvent.click(input);
  expect((input as HTMLInputElement).readOnly).toBe(false);
  expect(screen.queryByRole("dialog", { name: "关注，即刻开始。" })).toBeNull();
  fireEvent.change(input, { target: { value: "https://example.com/video" } });
  fireEvent.click(screen.getByRole("button", { name: "开始解析" }));
  await screen.findByRole("heading", { name: metadata.title });
  expect(parse).toHaveBeenCalledWith("https://example.com/video");
});

it("keeps the confirmed quote on a transient submission failure and sends it once on retry", async () => {
  const confirm = vi
    .mocked(api.confirmQuote)
    .mockRejectedValueOnce(new ApiError("NETWORK_ERROR", "提交暂时失败"))
    .mockResolvedValueOnce({ task_id: "job-1", status: "queued" });
  const legacy = vi.spyOn(api, "create");
  mount("/video/demo_one", savedStore());
  await screen.findByRole("radio", { name: /720P/ });
  fireEvent.click(screen.getByRole("button", { name: "下载视频" }));
  fireEvent.click(await screen.findByRole("button", { name: "确认并开始" }));
  await screen.findByText("提交暂时失败");
  fireEvent.click(screen.getByRole("button", { name: "确认并开始" }));
  await waitFor(() => expect(FakeSource.instances).toHaveLength(1));
  expect(confirm.mock.calls).toEqual([["quote-1"], ["quote-1"]]);
  expect(api.quote).toHaveBeenCalledTimes(1);
  expect(legacy).not.toHaveBeenCalled();
});

it("does not create a task when the user closes an allowance quote", async () => {
  mount("/video/demo_one", savedStore());
  await screen.findByRole("radio", { name: /720P/ });
  fireEvent.click(screen.getByRole("button", { name: "下载视频" }));
  fireEvent.click(await screen.findByRole("button", { name: "暂不开始" }));
  expect(screen.queryByRole("dialog", { name: "确认本次额度" })).toBeNull();
  expect(api.confirmQuote).not.toHaveBeenCalled();
  expect(FakeSource.instances).toHaveLength(0);
});
