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
import { allowances, quote, catalog } from "./billingFixtures";
import { api } from "../api";
import { ApiError, client } from "../api/client";
import { adminApi } from "../admin/api";
import { overview, storage } from "./adminFixtures";
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
  vi.spyOn(adminApi, "overview").mockResolvedValue(overview);
  vi.spyOn(adminApi, "storage").mockResolvedValue(storage);
  vi.spyOn(adminApi, "recentTasks").mockResolvedValue({
    items: [],
    total: 0,
    limit: 5,
    offset: 0,
  });
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
function mount(path = "/app", store = createWorkspace()) {
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
  fireEvent.change(screen.getByRole("textbox", { name: "视频链接" }), {
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
  const input = screen.getByRole("textbox", {
    name: "视频链接",
  }) as HTMLInputElement;
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
  act(() =>
    FakeSource.instances[0]!.emit({
      ...snapshot("running", 2),
      stage: "downloading",
    }),
  );
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe(
    "30",
  );
  act(() => FakeSource.instances[0]!.emit(snapshot("completed")));
  const download = await screen.findByRole("button", { name: "保存到本地" });
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.href).toBe(
        new URL(
          `${import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1"}/files/file-1/download`,
          window.location.origin,
        ).href,
      );
      expect(this.referrerPolicy).toBe("no-referrer");
    });
  fireEvent.click(download);
  await waitFor(() => expect(click).toHaveBeenCalledOnce());
  expect(FakeSource.instances[0]!.close).toHaveBeenCalled();
});

it("offers file upload after platform restrictions and preserves the video URL", async () => {
  vi.spyOn(api, "parse").mockRejectedValue(
    new ApiError(
      "SOURCE_ACCESS_RESTRICTED",
      "平台限制访问",
      "req_restricted",
      502,
    ),
  );
  mount();
  const url = "https://www.bilibili.com/video/BV1g2JX6nESP?t=3.6";
  fireEvent.change(screen.getByRole("textbox", { name: "视频链接" }), {
    target: { value: url },
  });
  fireEvent.click(screen.getByRole("button", { name: "开始解析" }));
  fireEvent.click(await screen.findByRole("button", { name: "改用上传文件" }));
  expect(screen.getByRole("button", { name: "选择文件" })).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "视频链接" }));
  expect(
    (screen.getByRole("textbox", { name: "视频链接" }) as HTMLInputElement)
      .value,
  ).toBe(url);
  expect(api.parse).toHaveBeenCalledOnce();
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
  fireEvent.click(await screen.findByRole("tab", { name: "AI 总结" }));
  expect(await screen.findByText("测试视频概述")).toBeTruthy();
  expect(screen.getAllByText("01:33").length).toBeGreaterThan(0);
  let finishCopy!: () => void;
  const writeText = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        finishCopy = resolve;
      }),
  );
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  fireEvent.click(screen.getByRole("button", { name: "复制总结" }));
  expect(screen.queryByText("总结已复制")).toBeNull();
  await act(async () => {
    finishCopy();
  });
  expect(await screen.findByText("总结已复制")).toBeTruthy();
  expect(writeText).toHaveBeenCalledWith(
    expect.stringContaining("测试视频概述"),
  );
  expect(writeText).toHaveBeenCalledWith(
    expect.stringContaining("检查事务边界"),
  );
  expect(screen.getByRole("heading", { name: "核心结论" })).toBeTruthy();
  writeText.mockRejectedValueOnce(new Error("permission denied"));
  fireEvent.click(screen.getByRole("button", { name: "复制总结" }));
  expect(await screen.findByText("复制失败，可下载总结后复制。")).toBeTruthy();
  expect(screen.queryByText("总结已复制")).toBeNull();
  expect(
    within(screen.getByRole("tabpanel", { name: "AI 总结" })).queryByText(
      "测试字幕内容",
    ),
  ).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "文稿" }));
  expect(screen.getByText("测试字幕内容")).toBeTruthy();
  const toggle = screen.getByRole("button", { name: /下载视频 \/ 提取音频/ });
  fireEvent.click(toggle);
  expect(
    document.querySelector(".content-download")?.hasAttribute("hidden"),
  ).toBe(false);
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
  fireEvent.click(screen.getByRole("link", { name: "导入新内容" }));
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
  expect(document.activeElement).toBe(
    screen.getByRole("textbox", { name: "视频链接" }),
  );
  expect(parse).not.toHaveBeenCalled();
});

it.each(["anonymous", "user", "disabled", "admin"])(
  "keeps cleanup in the admin area and settings permissions consistent for %s",
  async (role) => {
    const clean = vi.spyOn(api, "cleanup");
    authStore.clear();
    if (role !== "anonymous")
      authStore.accept({
        status: "authenticated",
        access_token: "local-fixture-token",
        user: {
          id: "settings-fixture",
          nickname: "测试",
          is_subscribed: true,
          role: role === "user" ? "user" : "admin",
          status: role === "disabled" ? "disabled" : "active",
        },
      });
    const home = mount();
    expect(screen.queryByText("PostgreSQL")).toBeNull();
    expect(api.ready).not.toHaveBeenCalled();
    home.unmount();
    mount("/settings");
    await screen.findByRole("heading", { name: "设置" });
    expect(screen.queryByRole("button", { name: /清理/ })).toBeNull();
    const link = screen.queryByRole("link", { name: "前往存储与服务 →" });
    if (role === "admin")
      expect(link).toHaveProperty("pathname", "/admin/system");
    else expect(link).toBeNull();
    expect(clean).not.toHaveBeenCalled();
  },
);

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
  expect(screen.queryByRole("button", { name: "任务历史" })).toBeNull();
  expect(document.querySelector(".app-header")).toBeNull();
  expect(api.history).not.toHaveBeenCalled();
  expect(api.allowances).not.toHaveBeenCalled();
});

it("does not poll service health for non-admin visitors", async () => {
  mount("/admin");
  expect(await screen.findByText("此页面仅供管理员访问。")).toBeTruthy();
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
  // Authenticate this isolated workspace before opening an account-scoped confirmation.
  act(() =>
    authStore.accept({
      status: "authenticated",
      access_token: "history-test",
      user: {
        id: "history-user",
        nickname: "测试用户",
        role: "user",
        status: "active",
        is_subscribed: true,
      },
    }),
  );
  mount();
  fireEvent.click(screen.getByRole("button", { name: "任务历史" }));
  const dialog = await screen.findByRole("dialog");
  await within(dialog).findByText("单条记录");
  fireEvent.click(
    within(dialog).getByRole("button", { name: "隐藏任务记录：单条记录" }),
  );
  await waitFor(() =>
    expect(within(dialog).queryByText("单条记录")).toBeNull(),
  );
  expect(remove).toHaveBeenCalledWith("history-0");
  fireEvent.click(within(dialog).getByRole("button", { name: "隐藏全部记录" }));
  const confirmation = await screen.findByRole("dialog", {
    name: "隐藏全部历史记录？",
  });
  expect(clear).not.toHaveBeenCalled();
  expect(within(confirmation).getByText(/当前账号共有 1 条/)).toBeTruthy();
  fireEvent.click(
    within(confirmation).getByRole("button", { name: "确认隐藏记录" }),
  );
  expect(await within(dialog).findByText("从第一份内容开始")).toBeTruthy();
  expect(clear).toHaveBeenCalledOnce();
  expect(cancel).not.toHaveBeenCalled();
});

it.each(["parse", "history", "login"])(
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
    if (entry === "parse") {
      fireEvent.change(screen.getByRole("textbox", { name: "视频链接" }), {
        target: { value: "https://example.com/video" },
      });
      fireEvent.click(screen.getByRole("button", { name: "开始解析" }));
    }
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
  const input = screen.getByRole("textbox", { name: "视频链接" });
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
  const input = screen.getByRole("textbox", { name: "视频链接" });
  fireEvent.click(input);
  expect((input as HTMLInputElement).readOnly).toBe(false);
  expect(screen.queryByRole("dialog", { name: "关注，即刻开始。" })).toBeNull();
  fireEvent.change(input, { target: { value: "https://example.com/video" } });
  fireEvent.click(screen.getByRole("button", { name: "开始解析" }));
  await screen.findByRole("heading", { name: metadata.title });
  expect(parse).toHaveBeenCalledWith(
    "https://example.com/video",
    expect.any(AbortSignal),
  );
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

const recoveryUser = {
  id: "recovery-user",
  nickname: "恢复用户",
  is_subscribed: true,
  role: "user",
  status: "active",
};
function recoveryLogin(id = recoveryUser.id) {
  authStore.accept({
    status: "authenticated",
    access_token: "recovery-token",
    user: { ...recoveryUser, id },
  });
}

it("restores a signed-in parsed video through real session bootstrap even with no server history", async () => {
  vi.mocked(authStore.restore).mockRestore();
  vi.mocked(authStore.hasAccess).mockRestore();
  const saved = createWorkspace();
  saved.switchUser(recoveryUser.id);
  saved.remember("https://example.com/video", metadata);
  saved.stopAll();
  const original = client.defaults.adapter;
  client.defaults.adapter = async (config) => ({
    data: { data: recoveryUser },
    status: 200,
    statusText: "OK",
    headers: {},
    config,
  });
  try {
    const view = mount("/video/demo_one");
    expect(
      await screen.findByRole("heading", { name: metadata.title }),
    ).toBeTruthy();
    await waitFor(() => expect(view.store.historyLoaded).toBe(true));
    expect(view.store.videos[0]?.metadata.download_options[0]?.id).toBe(
      "video_720_mp4",
    );
    expect(
      JSON.parse(
        localStorage.getItem(`videohub.videos.v1:${recoveryUser.id}`)!,
      ),
    ).toHaveLength(1);
    expect(screen.queryByText("重新连接你的内容。")).toBeNull();
    view.unmount();
  } finally {
    client.defaults.adapter = original;
  }
});

it("lets visitors prepare a link, preserves it through login and parses only after explicit submission", async () => {
  vi.mocked(authStore.hasAccess).mockRestore();
  vi.spyOn(authApi, "create").mockResolvedValue({
    scene_id: "prepared",
    mode: "verify_code",
    qr_url: "/wechat_qr.png",
    verify_code: "123456",
    expires_in: 300,
  });
  const parse = vi.spyOn(api, "parse").mockResolvedValue(metadata);
  mount();
  const input = screen.getByRole("textbox", {
    name: "视频链接",
  }) as HTMLInputElement;
  fireEvent.click(input);
  fireEvent.change(input, { target: { value: "https://example.com/video" } });
  expect(authStore.getState().modalOpen).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "开始解析" }));
  await screen.findByRole("dialog", { name: "关注，即刻开始。" });
  expect(parse).not.toHaveBeenCalled();
  act(() => recoveryLogin());
  expect(input.value).toBe("https://example.com/video");
  expect(parse).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "开始解析" }));
  expect(
    await screen.findByRole("heading", { name: metadata.title }),
  ).toBeTruthy();
});

it("offers session recovery after network failure without opening another login card", async () => {
  vi.mocked(authStore.restore).mockRestore();
  vi.mocked(authStore.hasAccess).mockRestore();
  const original = client.defaults.adapter;
  client.defaults.adapter = async () => {
    throw new Error("连接中断");
  };
  try {
    mount();
    const retry = await screen.findByRole("button", { name: "重新连接" });
    expect(
      (screen.getByRole("button", { name: "开始解析" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(authStore.requireLogin()).toBe(false);
    expect(authStore.getState().modalOpen).toBe(false);
    client.defaults.adapter = async (config) => ({
      data: { data: recoveryUser },
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    });
    fireEvent.click(retry);
    await screen.findByRole("button", { name: "账户菜单" });
    expect(screen.queryByRole("button", { name: "重新连接" })).toBeNull();
    expect(
      (screen.getByRole("button", { name: "开始解析" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  } finally {
    client.defaults.adapter = original;
  }
});

it("aborts parsing on account changes and ignores a late successful response", async () => {
  vi.mocked(authStore.hasAccess).mockRestore();
  recoveryLogin();
  let finish!: (value: VideoParseResult) => void;
  const parse = vi.spyOn(api, "parse").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const view = mount();
  fireEvent.change(screen.getByRole("textbox", { name: "视频链接" }), {
    target: { value: "https://example.com/video" },
  });
  fireEvent.click(screen.getByRole("button", { name: "开始解析" }));
  const signal = parse.mock.calls[0]![1]!;
  act(() => recoveryLogin("another-user"));
  expect(signal.aborted).toBe(true);
  await act(async () => finish(metadata));
  expect(screen.getByRole("textbox", { name: "视频链接" })).toBeTruthy();
  expect(view.store.videos).toEqual([]);
});

it("opens saved transcript and summary in the workspace without depending on task history", async () => {
  recoveryLogin();
  const detail = {
    id: "saved-one",
    transcript_id: "transcript-1",
    result_id: "summary-1",
    title: metadata.title,
    tags: [],
    excerpt: "收藏预览",
    source_type: "subtitle" as const,
    content_text: "保存的文稿",
    summary_text: "保存的总结",
    created_at: "2026-10-28",
    updated_at: "2026-10-28",
    video: {
      ...metadata,
      id: "video-1",
      source_url: "https://example.com/video",
    },
  };
  vi.spyOn(api, "library").mockResolvedValue({
    items: [detail],
    total: 1,
    limit: 20,
    offset: 0,
  });
  vi.spyOn(api, "libraryItem").mockResolvedValue(detail);
  vi.spyOn(api, "answers").mockResolvedValue({ items: [] });
  vi.spyOn(api, "transcript").mockResolvedValue(transcript);
  vi.spyOn(api, "result").mockResolvedValue(summary);
  const view = mount("/library");
  fireEvent.click(await screen.findByRole("button", { name: metadata.title }));
  fireEvent.click(await screen.findByRole("link", { name: "打开内容工作台" }));
  expect(await screen.findByText("测试视频概述")).toBeTruthy();
  expect(
    screen.getByRole("tab", { name: "AI 总结" }).getAttribute("aria-selected"),
  ).toBe("true");
  expect(view.store.tasks).toHaveLength(0);
  view.unmount();
  mount("/video/demo_one?tab=content");
  expect(await screen.findByText("测试视频概述")).toBeTruthy();
  fireEvent.click(screen.getByRole("tab", { name: "文稿" }));
  expect(screen.getByText("测试字幕内容")).toBeTruthy();
});

it("navigates from the account menu to personal allowances and reuses task history", async () => {
  vi.spyOn(api, "catalog").mockResolvedValue(catalog);
  authStore.accept({
    status: "authenticated",
    access_token: "test-token",
    user: {
      id: "account-navigation",
      nickname: "账户测试用户",
      is_subscribed: true,
      role: "user",
      status: "active",
    },
  });
  mount();
  fireEvent.click(screen.getByRole("button", { name: "账户菜单" }));
  fireEvent.click(screen.getByRole("link", { name: "个人中心" }));
  expect(await screen.findByRole("heading", { name: "个人中心" })).toBeTruthy();
  expect(document.title).toBe("VideoHub AI · 个人中心");
  await waitFor(() =>
    expect(document.activeElement).toBe(document.getElementById("main")),
  );
  expect(await screen.findByText("29 分 59 秒")).toBeTruthy();
  const shortcuts = screen.getByRole("navigation", {
    name: "个人中心常用入口",
  });
  fireEvent.click(within(shortcuts).getByRole("button", { name: "任务历史" }));
  expect(await screen.findByRole("dialog", { name: "任务历史" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "关闭任务历史" }));
  fireEvent.click(screen.getByRole("link", { name: "查看订阅方案" }));
  expect(await screen.findByRole("heading", { name: "免费体验" })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "我的额度" })).toBeNull();
});

it("retains video context across tabs and guides the question empty state back to transcript actions", async () => {
  mount("/video/demo_one?tab=questions", savedStore());
  const context = await screen.findByRole("region", { name: "当前内容" });
  expect(
    within(context).getByRole("heading", { name: metadata.title }),
  ).toBeTruthy();
  expect(within(context).getByText("测试作者")).toBeTruthy();
  expect(within(context).getByText("01:33")).toBeTruthy();
  expect(
    within(context)
      .getByRole("link", { name: "查看原视频" })
      .getAttribute("href"),
  ).toBe("https://example.com/video");
  fireEvent.click(screen.getByRole("button", { name: "前往文稿" }));
  expect(
    screen.getByRole("tab", { name: "文稿" }).getAttribute("aria-selected"),
  ).toBe("true");
  expect(
    screen.getByRole("heading", { name: "接下来，想如何整理？" }),
  ).toBeTruthy();
  expect(
    screen.getAllByRole("button", { name: "只提取文稿 阅读、搜索与导出原文" }),
  ).toHaveLength(1);
  for (const name of ["思维导图", "AI 总结", "内容问答"]) {
    fireEvent.click(screen.getByRole("tab", { name }));
    expect(
      within(context).getByRole("heading", { name: metadata.title }),
    ).toBeTruthy();
  }
});

it("starts the existing summary confirmation from an empty state and replaces guidance with progress", async () => {
  mount("/video/demo_one?tab=content", savedStore());
  fireEvent.click(await screen.findByRole("button", { name: "生成 AI 总结" }));
  expect(
    await screen.findByRole("dialog", { name: "确认本次额度" }),
  ).toBeTruthy();
  expect(api.quote).toHaveBeenCalledWith(
    "ai_summary",
    "https://example.com/video",
    undefined,
    expect.any(AbortSignal),
  );
  fireEvent.click(screen.getByRole("button", { name: "确认并开始" }));
  expect(
    await screen.findByRole("heading", { name: "正在梳理内容要点" }),
  ).toBeTruthy();
  expect(
    screen.queryByRole("heading", { name: "接下来，想如何整理？" }),
  ).toBeNull();
  expect(
    screen.getByRole("progressbar", { name: "生成 AI 总结" }),
  ).toBeTruthy();
});
