// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { api } from "../api";
import { client, ApiError } from "../api/client";
import * as clientApi from "../api/client";
import { authStore } from "../stores/auth";
import { billingStore } from "../stores/billing";
import { createWorkspace, type Workspace } from "../stores/workspace";
import { WorkspaceProvider } from "../hooks/useWorkspace";
import { useClipWorkspace } from "../hooks/useClipWorkspace";
import { clipStrategyValid, defaultClipStrategy } from "../lib/clips";
import { processingInput, taskLocation } from "../lib/content";
import type { ClipPlan, ClipExport } from "../types/clips";
import { useClipDelivery } from "../hooks/useClipDelivery";
import type { TaskSnapshot, VideoParseResult } from "../types/api";
import type { AllowanceQuote } from "../types/billing";
import VideoWorkspaceView from "../views/VideoWorkspaceView";
import { createTaskTracker } from "../lib/taskTracker";
import AllowanceConfirm from "../components/AllowanceConfirm";

vi.mock("../lib/taskTracker", async (original) => ({
  ...(await original<object>()),
  createTaskTracker: vi.fn(() => ({ stop: vi.fn() })),
}));
const metadata: VideoParseResult = {
  platform: "demo",
  platform_video_id: "one",
  title: "长内容示例",
  duration: 180,
  thumbnail: null,
  uploader: null,
  has_subtitles: true,
  subtitle_languages: ["zh"],
  download_options: [],
};
const proposal: AllowanceQuote = {
  quote_id: "quote-one",
  task_type: "ai_clip_plan",
  expires_at: "2099-01-01T00:00:00Z",
  maximum: {
    version: "v1",
    asr_seconds: 0,
    ai_generations: 1,
    limits: {
      video_seconds: 1200,
      file_bytes: 256 * 1024 ** 2,
      daily_egress_bytes: 1024 ** 3,
      download_concurrency: 1,
    },
  },
  note: "test",
};
function plan(id = "plan-one", videoId = "video-one"): ClipPlan {
  return {
    id,
    video_id: videoId,
    transcript_id: `text-${id}`,
    task_id: `task-${id}`,
    revision: 1,
    spec: {
      strategy: { count: 3, min_seconds: 30, max_seconds: 90, focus: "" },
      language: "zh",
    },
    outcome: "highlights",
    reason: "",
    source_basis: "subtitle",
    source: {
      video_id: videoId,
      source_kind: "link",
      media_type: "video",
      platform: "demo",
      platform_video_id: "one",
      title: "长内容示例",
      url: "https://example.com/video",
      duration: 180,
    },
    source_available: null,
    source_status: "public_link_requires_fetch",
    source_required: false,
    created_at:
      id === "plan-two" ? "2026-11-02T12:00:00Z" : "2026-11-01T12:00:00Z",
    candidates: [
      {
        id: "candidate-1",
        title: `完整观点 ${id}`,
        reason: "上下文完整",
        topic: "方法",
        quote: `候选原文 ${id}。需要保留完整条件。`,
        start: 0,
        end: 120,
        timing_precision: "coarse",
        needs_alignment: true,
      },
      {
        id: "candidate-2",
        title: "另一处观点",
        reason: "包含具体方法",
        topic: "实践",
        quote: "另一处完整原文",
        start: 0,
        end: 120,
        timing_precision: "coarse",
        needs_alignment: true,
      },
    ],
  };
}
function snapshot(
  status: TaskSnapshot["status"] = "queued",
  planId?: string,
): TaskSnapshot {
  return {
    task_id: "clip-job",
    video_id: "video-one",
    task_type: "ai_clip_plan",
    status,
    progress: status === "completed" ? 100 : 15,
    seq: status === "completed" ? 5 : 0,
    stage: status,
    error_code: null,
    error_message: null,
    cancel_requested: false,
    attempts: 1,
    input_payload: { title: "长内容示例" },
    result_payload: planId
      ? { clip_plan_id: planId, transcript_id: `text-${planId}` }
      : null,
    updated_at: "2026-11-02T12:00:00Z",
    created_at: "2026-11-02T12:00:00Z",
    started_at: null,
    finished_at: null,
    expires_at: null,
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}
function login(id = "clips-user") {
  authStore.accept({
    status: "authenticated",
    access_token: `token-${id}`,
    user: {
      id,
      nickname: "用户",
      role: "user",
      status: "active",
      is_subscribed: true,
    },
  });
}
let stores: Workspace[] = [];
beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => data.set(key, value),
    removeItem: (key: string) => data.delete(key),
    clear: () => data.clear(),
  });
  login();
  vi.stubEnv("VITE_CLIPS_ENABLED", "true");
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false })),
  );
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
  vi.spyOn(api, "clipPlanQuote").mockResolvedValue(proposal);
  vi.spyOn(api, "clipPlan").mockImplementation(async (id) => plan(id));
  vi.spyOn(api, "task").mockImplementation(async (id) => {
    if (id.startsWith("task-plan-")) {
      const artifact = plan(id.slice(5));
      return {
        ...snapshot("completed", artifact.id),
        task_id: id,
        created_at: artifact.created_at,
      };
    }
    return snapshot();
  });
  vi.spyOn(api, "confirmQuote").mockResolvedValue({
    task_id: "clip-job",
    status: "queued",
  });
  vi.spyOn(api, "transcript").mockImplementation(async (id) => ({
    id,
    video_id: "video-one",
    task_id: null,
    content_text: "文稿",
    content_hash: "h",
    content_version: "v1",
    asr_model: "none",
    created_at: "2026-11-01",
    source_type: "subtitle",
    language: "zh",
    segments: [{ start: 0, end: 120, text: "文稿原文" }],
  }));
  vi.spyOn(billingStore, "refresh").mockResolvedValue();
  vi.spyOn(billingStore, "loadUsage").mockResolvedValue();
});
afterEach(() => {
  cleanup();
  stores.forEach((store) => store.stopAll());
  stores = [];
  authStore.clear();
  billingStore.setUser(null);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});
function setup(withVideo = true) {
  const store = createWorkspace();
  store.switchUser("clips-user");
  stores.push(store);
  if (withVideo) store.remember("https://example.com/video", metadata);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <WorkspaceProvider store={store}>{children}</WorkspaceProvider>
  );
  return { store, wrapper };
}
function page(store: Workspace, url = "/video/demo_one?tab=clips") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <WorkspaceProvider store={store}>
        <div className="public-app">
          <Routes>
            <Route path="/video/:id" element={<VideoWorkspaceView />} />
          </Routes>
        </div>
      </WorkspaceProvider>
    </MemoryRouter>,
  );
}
function complete(store: Workspace, id = "plan-one") {
  const task = store.tasks.find((t) => t.id === "clip-job")!;
  const value = snapshot("completed", id);
  const callback = vi
    .mocked(createTaskTracker)
    .mock.calls.find((call) => call[0] === task.id)![1];
  act(() => {
    callback.onEvent(value);
    callback.onSnapshot(value);
  });
}
it("uses the dedicated analysis API and rejects generic quote/create fallbacks", async () => {
  vi.mocked(api.clipPlanQuote).mockRestore();
  const post = vi.spyOn(client, "post").mockResolvedValue({ data: proposal });
  const input = {
    transcript_id: "text",
    strategy: defaultClipStrategy(120),
    language: "zh",
  };
  expect(await api.clipPlanQuote(input)).toEqual(proposal);
  expect(post.mock.calls[0]?.slice(0, 2)).toEqual([
    "/clips/plans/quotes",
    input,
  ]);
  expect(() => api.quote("ai_clip_plan", "https://example.com")).toThrow(
    "专属报价",
  );
  expect(() => api.create("clips_export", "https://example.com")).toThrow(
    "专属报价",
  );
  expect(post).toHaveBeenCalledTimes(1);
});
it("defaults every source to semantic duration and rejects contradictory limits before HTTP", () => {
  expect(defaultClipStrategy(12)).toEqual({
    count: 3,
    duration_mode: "auto",
    min_seconds: null,
    max_seconds: null,
    focus: "",
  });
  expect(
    clipStrategyValid({ ...defaultClipStrategy(120), min_seconds: 91 }),
  ).toBe(false);
  expect(clipStrategyValid({ ...defaultClipStrategy(120), count: 6 })).toBe(
    false,
  );
  expect(
    clipStrategyValid({ ...defaultClipStrategy(120), focus: "字".repeat(201) }),
  ).toBe(false);
});
it("prefers transcript analysis even after the original upload has expired", () => {
  const { store } = setup();
  const video = store.findContent("demo_one")!;
  video.source = { kind: "upload", mediaType: "video", status: "expired" };
  expect(processingInput(video, "ai_clip_plan", "owned-text")).toEqual({
    transcript_id: "owned-text",
  });
});
it("does not create a task until consent; then registers one globally and reads completed candidates", async () => {
  const { store } = setup();
  page(store);
  await screen.findByRole("button", { name: "查看分析额度" });
  fireEvent.click(screen.getByRole("button", { name: "查看分析额度" }));
  await screen.findByRole("dialog");
  expect(api.confirmQuote).not.toHaveBeenCalled();
  expect(api.clipPlanQuote).toHaveBeenCalledWith(
    expect.objectContaining({ url: "https://example.com/video" }),
    expect.any(AbortSignal),
  );
  fireEvent.click(screen.getByRole("button", { name: "确认并开始" }));
  await waitFor(() => expect(store.tasks[0]?.kind).toBe("ai_clip_plan"));
  complete(store);
  await within(
    await screen.findByRole("list", { name: "精彩候选" }),
  ).findByRole("heading", { name: "完整观点 plan-one", level: 3 });
  expect(createTaskTracker).toHaveBeenCalledTimes(1);
  expect(screen.getByText("时间仅供参考，生成视频时需精确定位。")).toBeTruthy();
});
it("blocks double confirmation while the original request is unresolved", async () => {
  const pending = deferred<{ task_id: string; status: "queued" }>();
  vi.mocked(api.confirmQuote).mockReturnValue(pending.promise);
  const { store } = setup();
  page(store);
  fireEvent.click(await screen.findByRole("button", { name: "查看分析额度" }));
  await screen.findByRole("dialog");
  const submit = screen.getByRole("button", { name: "确认并开始" });
  fireEvent.click(submit);
  fireEvent.click(submit);
  expect(api.confirmQuote).toHaveBeenCalledTimes(1);
  expect(store.findContent("demo_one")?.pendingClipQuoteId).toBe(
    proposal.quote_id,
  );
  await act(async () =>
    pending.resolve({ task_id: "clip-job", status: "queued" }),
  );
  await waitFor(() =>
    expect(store.findContent("demo_one")?.pendingClipQuoteId).toBeUndefined(),
  );
});
it("persists only the uncertain quote ID and recovers the same consent after a lost ACK", async () => {
  vi.mocked(api.confirmQuote).mockRejectedValueOnce(
    new ApiError("NETWORK_ERROR", "连接中断"),
  );
  const { store } = setup();
  page(store);
  fireEvent.click(await screen.findByRole("button", { name: "查看分析额度" }));
  await screen.findByRole("dialog");
  fireEvent.click(screen.getByRole("button", { name: "确认并开始" }));
  await waitFor(() =>
    expect(
      screen.getAllByRole("button", { name: "继续检查原任务" }).length,
    ).toBeGreaterThan(0),
  );
  fireEvent.click(screen.getByRole("button", { name: "暂不开始" }));
  fireEvent.click(screen.getByRole("button", { name: "继续检查原任务" }));
  await waitFor(() => expect(store.tasks.length).toBe(1));
  expect(api.confirmQuote).toHaveBeenNthCalledWith(1, proposal.quote_id);
  expect(api.confirmQuote).toHaveBeenNthCalledWith(2, proposal.quote_id);
  expect(api.clipPlanQuote).toHaveBeenCalledTimes(1);
  expect(localStorage.getItem("videohub.videos.v1:clips-user")).not.toContain(
    '"maximum"',
  );
});
it("restores the pending confirmation after reload without obtaining a fresh quote", async () => {
  const { store } = setup();
  store.setClipConfirmation("demo_one", proposal.quote_id);
  store.stopAll();
  const restored = createWorkspace();
  restored.switchUser("clips-user");
  stores.push(restored);
  page(restored);
  fireEvent.click(
    await screen.findByRole("button", { name: "继续检查原任务" }),
  );
  await waitFor(() => expect(restored.tasks[0]?.id).toBe("clip-job"));
  expect(api.clipPlanQuote).not.toHaveBeenCalled();
  expect(api.confirmQuote).toHaveBeenCalledWith(proposal.quote_id);
});
it("restores a read-only private plan on a fresh deep link with creation disabled", async () => {
  vi.stubEnv("VITE_CLIPS_ENABLED", "false");
  vi.mocked(api.clipPlan).mockResolvedValue({
    ...plan(),
    source: {
      ...plan().source,
      source_kind: "upload",
      platform: null,
      platform_video_id: null,
      url: null,
    },
    source_available: false,
    source_required: true,
    source_status: "source_required",
  });
  const { store } = setup(false);
  page(store, "/video/content_video-one?tab=clips&clip_plan=plan-one");
  await within(
    await screen.findByRole("list", { name: "精彩候选" }),
  ).findByRole("heading", { name: "完整观点 plan-one", level: 3 });
  expect(store.findContent("content_video-one")?.source).toMatchObject({
    status: "unknown",
    kind: "upload",
  });
  expect(screen.queryByRole("button", { name: "查看分析额度" })).toBeNull();
  expect(api.clipPlanQuote).not.toHaveBeenCalled();
});
it("does not mix a deep-linked foreign plan into an existing video's context", async () => {
  const foreign = plan("plan-foreign", "video-foreign");
  vi.mocked(api.clipPlan).mockResolvedValue(foreign);
  const { store } = setup();
  store.findContent("demo_one")!.contentId = "video-one";
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-foreign");
  await waitFor(() =>
    expect(
      screen.getAllByText(/切片方案与当前内容不一致/).length,
    ).toBeGreaterThan(0),
  );
  expect(screen.queryByText(foreign.candidates[0].quote)).toBeNull();
  expect(store.findContent("demo_one")?.clipPlanId).toBeUndefined();
});
it("checks export/plan identity before restoring a historical result without file_ids", async () => {
  vi.spyOn(api, "clipExport").mockResolvedValue({
    id: "export-one",
    plan_id: "plan-one",
    task_id: "export-job",
    revision: 1,
    items: [],
  });
  const { store } = setup(false);
  page(store, "/video/content_video-one?tab=clips&clip_export=export-one");
  await within(
    await screen.findByRole("list", { name: "精彩候选" }),
  ).findByRole("heading", { name: "完整观点 plan-one", level: 3 });
  expect(api.clipPlan).toHaveBeenCalledWith(
    "plan-one",
    expect.any(AbortSignal),
  );
});
it("rejects inconsistent export and plan IDs without fetching the unrelated plan", async () => {
  vi.spyOn(api, "clipExport").mockResolvedValue({
    id: "export-one",
    plan_id: "plan-two",
    task_id: "export-job",
    revision: 1,
    items: [],
  });
  const { store } = setup();
  page(
    store,
    "/video/demo_one?tab=clips&clip_export=export-one&clip_plan=plan-one",
  );
  await waitFor(() =>
    expect(screen.getAllByText(/切片结果与方案不一致/).length).toBeGreaterThan(
      0,
    ),
  );
  expect(api.clipPlan).not.toHaveBeenCalled();
});
it("tracks a direct active task once without fabricating source metadata, then restores its result", async () => {
  const { store } = setup(false);
  page(store, "/video/content_video-one?tab=clips&clip_task=clip-job");
  await waitFor(() => expect(store.tasks[0]?.id).toBe("clip-job"));
  expect(store.videos).toHaveLength(0);
  complete(store);
  await within(
    await screen.findByRole("list", { name: "精彩候选" }),
  ).findByRole("heading", { name: "完整观点 plan-one", level: 3 });
  expect(createTaskTracker).toHaveBeenCalledTimes(1);
});
it("ignores a late resource response after account switch", async () => {
  const pending = deferred<ClipPlan>();
  vi.mocked(api.clipPlan)
    .mockReturnValueOnce(pending.promise)
    .mockRejectedValue(
      new ApiError("CLIP_PLAN_NOT_FOUND", "无权查看", "", 404),
    );
  const { store, wrapper } = setup(false);
  const { result } = renderHook(
    () => useClipWorkspace("content_video-one", { plan: "plan-one" }, true),
    { wrapper },
  );
  await waitFor(() => expect(api.clipPlan).toHaveBeenCalled());
  act(() => {
    login("other-user");
    store.switchUser("other-user");
  });
  await act(async () => pending.resolve(plan()));
  expect(result.current.plan).toBeUndefined();
  expect(store.videos).toHaveLength(0);
});
it("does not replace the reading plan until the user accepts newer candidates", async () => {
  const { store, wrapper } = setup();
  store.rememberClipPlan(plan());
  const { result } = renderHook(
    () => useClipWorkspace("demo_one", { plan: "plan-one" }, true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.plan?.id).toBe("plan-one"));
  act(() => store.adoptClipTask(snapshot("completed", "plan-two"), "demo_one"));
  await waitFor(() => expect(result.current.pending?.id).toBe("plan-two"));
  expect(result.current.plan?.id).toBe("plan-one");
  act(() => result.current.acceptPending());
  expect(result.current.plan?.id).toBe("plan-two");
  expect(store.findContent("demo_one")?.clipPlanId).toBe("plan-two");
});
it("keeps coarse overlapping candidates and pins original-text navigation to the plan transcript", async () => {
  const { store } = setup();
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  await screen.findByRole("list", { name: "精彩候选" });
  expect(
    within(screen.getByRole("list", { name: "精彩候选" })).getAllByRole(
      "listitem",
    ),
  ).toHaveLength(2);
  expect(
    screen.getByRole("link", { name: "查看对应文稿" }).getAttribute("href"),
  ).toContain("transcript=text-plan-one");
  expect(screen.getAllByRole("checkbox")).toHaveLength(2);
});
it("opens the original text in a narrow-screen dialog", async () => {
  vi.mocked(window.matchMedia).mockReturnValue({
    matches: true,
  } as MediaQueryList);
  const { store } = setup();
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  fireEvent.click(
    await screen.findByRole("button", { name: "查看原文：另一处观点" }),
  );
  const dialog = await screen.findByRole("dialog");
  expect(
    within(dialog).getByRole("heading", { name: "另一处观点" }),
  ).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("button", { name: "关闭片段原文" }));
  expect(screen.queryByRole("dialog")).toBeNull();
});
it("treats no highlights as a normal no-charge outcome and keeps the original text accessible", async () => {
  vi.mocked(api.clipPlan).mockResolvedValue({
    ...plan(),
    outcome: "no_highlights",
    candidates: [],
    reason: "当前内容依赖前文",
  });
  const { store } = setup();
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  await screen.findByText("没有找到符合当前条件的片段");
  expect(screen.getByText("当前内容依赖前文")).toBeTruthy();
  expect(screen.getByText("本次分析不扣 AI 额度。")).toBeTruthy();
});
it("hides the creation tab by default without disabling the other workspace areas", () => {
  vi.stubEnv("VITE_CLIPS_ENABLED", "false");
  const { store } = setup();
  page(store, "/video/demo_one");
  expect(screen.getAllByRole("tab")).toHaveLength(4);
  expect(api.clipPlanQuote).not.toHaveBeenCalled();
  expect(api.clipPlan).not.toHaveBeenCalled();
});
it("uses clip result IDs in history links instead of generic AI result parameters", () => {
  const value = snapshot("completed", "plan-one");
  expect(
    taskLocation({
      id: value.task_id,
      videoKey: "demo_one",
      snapshot: value,
      kind: "ai_clip_plan",
    } as never),
  ).toContain("clip_plan=plan-one");
  expect(
    taskLocation({
      id: value.task_id,
      videoKey: "demo_one",
      snapshot: { ...value, result_payload: { clip_export_id: "export-one" } },
      kind: "clips_export",
    } as never),
  ).toContain("clip_export=export-one");
});
it("keeps idempotent recovery available even when an uncertain quote's display expiry has passed", () => {
  render(
    <MemoryRouter>
      <AllowanceConfirm
        quote={{ ...proposal, expires_at: "2020-01-01T00:00:00Z" }}
        label="寻找精彩片段"
        pending={false}
        error={null}
        confirmationUncertain
        onConfirm={() => {}}
        onClose={() => {}}
        onRefresh={() => {}}
      />
    </MemoryRouter>,
  );
  expect(screen.getByRole("button", { name: "继续检查原任务" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "重新获取报价" })).toBeNull();
});
it("offers an explicit fresh quote after server invalidation, without silently confirming another operation", async () => {
  vi.mocked(api.confirmQuote).mockRejectedValueOnce(
    new ApiError("CLIP_REQUOTE_REQUIRED", "缓存失效", "req_one", 409),
  );
  const { store } = setup();
  page(store);
  fireEvent.click(await screen.findByRole("button", { name: "查看分析额度" }));
  await screen.findByRole("dialog");
  fireEvent.click(screen.getByRole("button", { name: "确认并开始" }));
  const refresh = await screen.findByRole("button", { name: "重新获取报价" });
  expect(store.findContent("demo_one")?.pendingClipQuoteId).toBeUndefined();
  expect(api.clipPlanQuote).toHaveBeenCalledTimes(1);
  vi.mocked(api.clipPlanQuote).mockResolvedValue({
    ...proposal,
    quote_id: "quote-two",
  });
  fireEvent.click(refresh);
  await waitFor(() => expect(api.clipPlanQuote).toHaveBeenCalledTimes(2));
  expect(api.confirmQuote).toHaveBeenCalledTimes(1);
});
it("discards a quote arriving after the account has changed", async () => {
  const pending = deferred<AllowanceQuote>();
  vi.mocked(api.clipPlanQuote).mockReturnValue(pending.promise);
  const { store } = setup();
  page(store);
  fireEvent.click(await screen.findByRole("button", { name: "查看分析额度" }));
  act(() => {
    login("other-user");
    store.switchUser("other-user");
  });
  await act(async () => pending.resolve(proposal));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(api.confirmQuote).not.toHaveBeenCalled();
});
it("uses the retained plan's transcript for a new analysis when its original file is gone", async () => {
  vi.mocked(api.clipPlan).mockResolvedValue({
    ...plan(),
    source: {
      ...plan().source,
      source_kind: "upload",
      platform: null,
      platform_video_id: null,
      url: null,
    },
    source_status: "source_required",
    source_required: true,
    source_available: false,
  });
  const { store } = setup(false);
  page(store, "/video/content_video-one?tab=clips&clip_plan=plan-one");
  fireEvent.click(await screen.findByRole("button", { name: "调整查找条件" }));
  fireEvent.click(screen.getByRole("button", { name: "查看分析额度" }));
  await screen.findByRole("dialog");
  expect(api.clipPlanQuote).toHaveBeenCalledWith(
    expect.objectContaining({ transcript_id: "text-plan-one" }),
    expect.any(AbortSignal),
  );
  expect(api.clipPlanQuote).not.toHaveBeenCalledWith(
    expect.objectContaining({ upload_id: expect.anything() }),
    expect.anything(),
  );
});
it("reports incomplete response data without showing partial candidates or creating source records", async () => {
  vi.mocked(api.clipPlan).mockResolvedValue({
    ...plan(),
    source: undefined,
  } as unknown as ClipPlan);
  const { store } = setup(false);
  page(store, "/video/content_video-one?tab=clips&clip_plan=plan-one");
  await waitFor(() =>
    expect(
      screen.getAllByText("切片方案数据不完整，请重新加载").length,
    ).toBeGreaterThan(0),
  );
  expect(store.videos).toHaveLength(0);
  expect(screen.queryByRole("list", { name: "精彩候选" })).toBeNull();
});
it("does not infer a cache hit from a zero-amount quote; uses the completed server result flag", async () => {
  const { store } = setup();
  const done = snapshot("completed", "plan-one");
  done.result_payload!.cache_hit = true;
  store.adoptClipTask(done, "demo_one");
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  await screen.findByText("已取回已有候选，本次未扣 AI 额度。");
  expect(api.confirmQuote).not.toHaveBeenCalled();
});
it("closes the mobile original-text dialog when its bound transcript is opened", async () => {
  vi.mocked(window.matchMedia).mockReturnValue({
    matches: true,
  } as MediaQueryList);
  const { store } = setup();
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  fireEvent.click(
    await screen.findByRole("button", { name: "查看原文：另一处观点" }),
  );
  fireEvent.click(
    within(await screen.findByRole("dialog")).getByRole("link", {
      name: "查看对应文稿",
    }),
  );
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(
    screen.getByRole("tab", { name: "文稿" }).getAttribute("aria-selected"),
  ).toBe("true");
});
it("shows a login recovery action instead of an endless resource loader for an anonymous deep link", async () => {
  const { store } = setup(false);
  act(() => {
    authStore.clear();
    store.switchUser(null);
  });
  page(store, "/video/content_video-one?tab=clips&clip_plan=plan-one");
  await screen.findByRole("heading", { name: "登录后查看切片方案" });
  expect(screen.getByRole("button", { name: "登录" })).toBeTruthy();
  expect(api.clipPlan).not.toHaveBeenCalled();
});
it("offers newer candidates even when the first successful analysis was opened through a task deep link", async () => {
  const { store, wrapper } = setup();
  store.adoptClipTask(snapshot("completed", "plan-one"), "demo_one");
  const { result } = renderHook(
    () => useClipWorkspace("demo_one", { task: "clip-job" }, true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.plan?.id).toBe("plan-one"));
  act(() =>
    store.adoptClipTask(
      { ...snapshot("completed", "plan-two"), task_id: "second-job" },
      "demo_one",
    ),
  );
  await waitFor(() => expect(result.current.pending?.id).toBe("plan-two"));
  expect(result.current.plan?.id).toBe("plan-one");
});
it("opens the current plan's retained transcript when switching from clips to the transcript tab", async () => {
  const { store } = setup();
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  await screen.findByRole("list", { name: "精彩候选" });
  fireEvent.click(screen.getByRole("tab", { name: "文稿" }));
  await waitFor(() =>
    expect(api.transcript).toHaveBeenCalledWith(
      "text-plan-one",
      expect.any(AbortSignal),
    ),
  );
  expect(
    screen.getByRole("tab", { name: "文稿" }).getAttribute("aria-selected"),
  ).toBe("true");
  expect(api.confirmQuote).not.toHaveBeenCalled();
});

it("offers an older cached artifact from the latest request and keeps acceptance stable on reload and late completion", async () => {
  const { store, wrapper } = setup();
  store.rememberClipPlan(plan("plan-two"));
  const { result } = renderHook(
    () => useClipWorkspace("demo_one", { plan: "plan-two" }, true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.plan?.id).toBe("plan-two"));
  const cached = {
    ...snapshot("completed", "plan-one"),
    task_id: "new-cache-request",
    created_at: "2026-11-03T12:00:00Z",
    result_payload: { clip_plan_id: "plan-one", cache_hit: true },
  };
  act(() => store.adoptClipTask(cached, "demo_one"));
  await waitFor(() => expect(result.current.pending?.id).toBe("plan-one"));
  expect(result.current.plan?.id).toBe("plan-two");
  act(() => result.current.acceptPending());
  act(() => {
    store.adoptClipTask(cached, "demo_one");
    store.adoptClipTask(
      { ...snapshot("completed", "plan-two"), task_id: "late-old-request" },
      "demo_one",
    );
    result.current.reload();
  });
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.plan?.id).toBe("plan-one");
  expect(result.current.pending).toBeUndefined();
  expect(result.current.error).toBeNull();
});

it("accepts an old cached artifact on a task deep link without revalidating it against the original task", async () => {
  const { store, wrapper } = setup();
  store.adoptClipTask(snapshot("completed", "plan-two"), "demo_one");
  const { result } = renderHook(
    () => useClipWorkspace("demo_one", { task: "clip-job" }, true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.plan?.id).toBe("plan-two"));
  act(() =>
    store.adoptClipTask(
      {
        ...snapshot("completed", "plan-one"),
        task_id: "cached-request",
        created_at: "2026-11-03T12:00:00Z",
      },
      "demo_one",
    ),
  );
  await waitFor(() => expect(result.current.pending?.id).toBe("plan-one"));
  act(() => result.current.acceptPending());
  act(() => result.current.reload());
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.plan?.id).toBe("plan-one");
  expect(result.current.pending).toBeUndefined();
  expect(result.current.error).toBeNull();
});

it("keeps the latest analysis ahead of later exports and unrelated videos while restoring an export deep link", async () => {
  const { store, wrapper } = setup();
  vi.spyOn(api, "clipExport").mockResolvedValue({
    id: "export-old",
    plan_id: "plan-one",
  } as never);
  store.adoptClipTask(
    { ...snapshot("completed", "plan-two"), task_id: "new-analysis" },
    "demo_one",
  );
  store.adoptClipTask(
    {
      ...snapshot("completed", "plan-one"),
      task_id: "later-export",
      task_type: "clips_export",
      created_at: "2026-11-03T12:00:00Z",
      result_payload: {
        clip_export_id: "export-old",
        clip_plan_id: "plan-one",
      },
    },
    "demo_one",
  );
  store.adoptClipTask(
    {
      ...snapshot("completed", "foreign-plan"),
      task_id: "foreign-analysis",
      video_id: "foreign-video",
      created_at: "2026-11-04T12:00:00Z",
    },
    "foreign_video",
  );
  const { result } = renderHook(
    () => useClipWorkspace("demo_one", { exported: "export-old" }, true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.pending?.id).toBe("plan-two"));
  expect(result.current.plan?.id).toBe("plan-one");
  expect(api.clipPlan).not.toHaveBeenCalledWith(
    "foreign-plan",
    expect.anything(),
  );
  act(() => result.current.acceptPending());
  act(() => result.current.reload());
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.plan?.id).toBe("plan-two");
  expect(result.current.error).toBeNull();
});

it("shows the same-plan cache acknowledgement even if an older analysis arrives later", async () => {
  const { store } = setup();
  store.rememberClipPlan(plan("plan-two"));
  store.adoptClipTask(
    {
      ...snapshot("completed", "plan-two"),
      task_id: "new-cache-request",
      created_at: "2026-11-03T12:00:00Z",
      result_payload: { clip_plan_id: "plan-two", cache_hit: true },
    },
    "demo_one",
  );
  // Arrival order must not control the completed-result feedback.
  store.adoptClipTask(
    { ...snapshot("completed", "plan-one"), task_id: "late-old-request" },
    "demo_one",
  );
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-two");
  await screen.findByText("已取回已有候选，本次未扣 AI 额度。");
  expect(screen.queryByRole("button", { name: "查看新候选" })).toBeNull();
});

it.each([
  "success",
  "definite failure",
  "network failure",
  "HTTP timeout",
] as const)(
  "settles an unmounted confirmation globally: %s",
  async (outcome) => {
    const pending = deferred<{ task_id: string; status: "queued" }>();
    vi.mocked(api.confirmQuote).mockReturnValue(pending.promise);
    const { store } = setup();
    const rendered = page(store);
    fireEvent.click(
      await screen.findByRole("button", { name: "查看分析额度" }),
    );
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "确认并开始",
      }),
    );
    rendered.unmount();
    await act(async () => {
      if (outcome === "success")
        pending.resolve({ task_id: "clip-job", status: "queued" });
      else
        pending.reject(
          new ApiError(
            outcome === "definite failure"
              ? "QUOTE_EXPIRED"
              : "REQUEST_TIMEOUT",
            "test",
            "",
            outcome === "definite failure"
              ? 409
              : outcome === "HTTP timeout"
                ? 408
                : undefined,
          ),
        );
    });
    const uncertain =
      outcome === "network failure" || outcome === "HTTP timeout";
    expect(store.findContent("demo_one")?.pendingClipQuoteId).toBe(
      uncertain ? proposal.quote_id : undefined,
    );
    expect(store.tasks).toHaveLength(outcome === "success" ? 1 : 0);
    const persisted = JSON.parse(
      localStorage.getItem("videohub.videos.v1:clips-user")!,
    );
    expect(persisted[0].pendingClipQuoteId).toBe(
      uncertain ? proposal.quote_id : undefined,
    );
  },
);

it.each(["success", "failure"] as const)(
  "does not clear a newer quote on an old %s response",
  async (outcome) => {
    const pending = deferred<{ task_id: string; status: "queued" }>();
    vi.mocked(api.confirmQuote).mockReturnValue(pending.promise);
    const { store } = setup();
    const request = store.create(
      store.findContent("demo_one")!,
      "ai_clip_plan",
      "切片",
      undefined,
      "old-quote",
    );
    const settled = request.catch(() => undefined);
    store.setClipConfirmation("demo_one", "new-quote");
    if (outcome === "success")
      pending.resolve({ task_id: "clip-job", status: "queued" });
    else pending.reject(new ApiError("QUOTE_EXPIRED", "test", "", 409));
    await settled;
    expect(store.findContent("demo_one")?.pendingClipQuoteId).toBe("new-quote");
  },
);

it.each(["switch", "relogin"] as const)(
  "fences a confirmation response after %s",
  async (mode) => {
    const pending = deferred<{ task_id: string; status: "queued" }>();
    vi.mocked(api.confirmQuote).mockReturnValue(pending.promise);
    const { store } = setup();
    const request = store.create(
      store.findContent("demo_one")!,
      "ai_clip_plan",
      "切片",
      undefined,
      "old-quote",
    );
    const settled = request.catch((error: ApiError) => error.code);
    store.switchUser(null);
    store.switchUser(mode === "switch" ? "other-user" : "clips-user");
    store.remember("https://example.com/video", metadata);
    store.setClipConfirmation("demo_one", "new-session-quote");
    pending.resolve({ task_id: "old-session-task", status: "queued" });
    expect(await settled).toBe("AUTH_CHANGED");
    expect(store.tasks).toHaveLength(0);
    expect(store.findContent("demo_one")?.pendingClipQuoteId).toBe(
      "new-session-quote",
    );
  },
);

it("preserves the uncertain marker during an in-flight recovery guard and deduplicates acknowledged recovery", async () => {
  const pending = deferred<{ task_id: string; status: "queued" }>();
  vi.mocked(api.confirmQuote).mockReturnValue(pending.promise);
  const { store } = setup();
  const video = store.findContent("demo_one")!;
  store.setClipConfirmation(video.key, proposal.quote_id);
  const first = store.recoverClipConfirmation(video);
  await expect(store.recoverClipConfirmation(video)).rejects.toMatchObject({
    code: "TASK_ALREADY_RUNNING",
  });
  expect(video.pendingClipQuoteId).toBe(proposal.quote_id);
  pending.resolve({ task_id: "clip-job", status: "queued" });
  await first;
  expect(video.pendingClipQuoteId).toBeUndefined();
  store.setClipConfirmation(video.key, proposal.quote_id);
  await store.recoverClipConfirmation(video);
  expect(store.tasks).toHaveLength(1);
  expect(video.pendingClipQuoteId).toBeUndefined();
});

it("does not offer older local history over a newer deep-linked plan whose task is not stored locally", async () => {
  const { store, wrapper } = setup();
  store.adoptClipTask(
    {
      ...snapshot("completed", "plan-one"),
      task_id: "old-task",
      created_at: "2026-11-01T12:00:00Z",
    },
    "demo_one",
  );
  const { result } = renderHook(
    () => useClipWorkspace("demo_one", { plan: "plan-two" }, true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.plan?.id).toBe("plan-two");
  expect(result.current.pending).toBeUndefined();
  expect(api.task).toHaveBeenCalledWith(
    "task-plan-two",
    expect.any(AbortSignal),
  );
  expect(result.current.error).toBeNull();
});

it("refreshes the accepted plan's source state without reverting to the original deep link", async () => {
  const { store, wrapper } = setup();
  const { result } = renderHook(
    () => useClipWorkspace("demo_one", { plan: "plan-one" }, true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.plan?.id).toBe("plan-one"));
  act(() => store.adoptClipTask(snapshot("completed", "plan-two"), "demo_one"));
  await waitFor(() => expect(result.current.pending?.id).toBe("plan-two"));
  act(() => result.current.acceptPending());
  vi.mocked(api.clipPlan).mockImplementation(async (id) => ({
    ...plan(id),
    source_status: "source_required",
  }));
  act(() => result.current.reload());
  await waitFor(() =>
    expect(result.current.plan?.source_status).toBe("source_required"),
  );
  expect(result.current.plan?.id).toBe("plan-two");
  expect(result.current.pending).toBeUndefined();
});

const exportProposal: AllowanceQuote = {
  ...proposal,
  quote_id: "export-quote",
  task_type: "clips_export",
  maximum: {
    ...proposal.maximum,
    ai_generations: 0,
    clip_limits: { max_daily_exports: 10, max_daily_plan_exports: 3 },
  },
};
function exported(id = "export-one", planId = "plan-one"): ClipExport {
  return {
    id,
    task_id: "export-job",
    plan_id: planId,
    revision: 1,
    items: plan(planId)
      .candidates.map((candidate, index) => ({
        candidate_id: candidate.id,
        file_id: `${id}-file-${index}`,
        title: `服务端顺序 ${index}`,
        start: index * 8,
        end: index * 8 + 5.25,
        size_bytes: 2048,
        expires_at: "2099-01-01T00:00:00Z",
        download_available: true,
      }))
      .reverse(),
  };
}
function exportSetup() {
  vi.spyOn(api, "clipExportQuote").mockResolvedValue(exportProposal);
  vi.spyOn(api, "clipExport").mockImplementation(async (id) => exported(id));
  const value = setup();
  value.store.rememberClipPlan(plan());
  return value;
}
async function selectFirstAndQuote() {
  fireEvent.click(
    await screen.findByRole("checkbox", {
      name: "选择片段：完整观点 plan-one",
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "查看生成说明" }));
  return await screen.findByRole("dialog", { name: "确认生成视频" });
}
function exportSnapshot(
  id = "export-one",
  taskId = "export-job",
): TaskSnapshot {
  return {
    ...snapshot("completed", "plan-one"),
    task_id: taskId,
    task_type: "clips_export",
    result_payload: { clip_plan_id: "plan-one", clip_export_id: id },
  };
}
it("F2 uses only the dedicated export quote API", async () => {
  const post = vi
    .spyOn(client, "post")
    .mockResolvedValue({ data: exportProposal });
  const input = {
    plan_id: "plan-one",
    revision: 1,
    candidate_ids: ["candidate-1"],
    profile: "video_720_mp4" as const,
  };
  await api.clipExportQuote(input);
  expect(post).toHaveBeenCalledWith("/clips/exports/quotes", input);
});
it("F2 keeps selection separate from reading and freezes an explicit export confirmation", async () => {
  const { store } = exportSetup();
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  const first = await screen.findByRole("checkbox", {
    name: "选择片段：完整观点 plan-one",
  });
  expect(
    (screen.getByRole("button", { name: "查看生成说明" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "查看原文：另一处观点" }));
  expect((first as HTMLInputElement).checked).toBe(false);
  const dialog = await selectFirstAndQuote();
  expect(api.confirmQuote).not.toHaveBeenCalled();
  expect(api.clipExportQuote).toHaveBeenCalledWith(
    {
      plan_id: "plan-one",
      revision: 1,
      candidate_ids: ["candidate-1"],
      profile: "video_720_mp4",
    },
    expect.any(AbortSignal),
  );
  expect(within(dialog).getByText("MP4 · 最高 720p · 保留原比例")).toBeTruthy();
  expect(within(dialog).getByText(/失败或取消也可能占用处理次数/)).toBeTruthy();
  expect(within(dialog).queryByText("免费取回已有结果")).toBeNull();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认并生成" }));
  await waitFor(() => expect(store.tasks).toHaveLength(1));
  expect(store.tasks[0].kind).toBe("clips_export");
  expect(store.tasks[0].clipPlanId).toBe("plan-one");
  expect(store.findContent("demo_one")?.pendingClipExport).toBeUndefined();
});
it("F2 discards an export quote when the user changes the selected set", async () => {
  const pending = deferred<AllowanceQuote>();
  const { store } = exportSetup();
  vi.mocked(api.clipExportQuote).mockReturnValue(pending.promise);
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  fireEvent.click(
    await screen.findByRole("checkbox", {
      name: "选择片段：完整观点 plan-one",
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "查看生成说明" }));
  fireEvent.click(
    screen.getByRole("checkbox", { name: "选择片段：另一处观点" }),
  );
  await act(async () => pending.resolve(exportProposal));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(api.confirmQuote).not.toHaveBeenCalled();
  expect(store.findContent("demo_one")?.clipSelection?.candidate_ids).toEqual([
    "candidate-1",
    "candidate-2",
  ]);
});
it("F2 allows cache-aware quotes without a source and hides video selection for audio", async () => {
  const { store } = exportSetup();
  vi.mocked(api.clipPlan).mockResolvedValue({
    ...plan(),
    source_required: true,
    source_available: false,
    source_status: "source_required",
  });
  const rendered = page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  await selectFirstAndQuote();
  expect(api.clipExportQuote).toHaveBeenCalledTimes(1);
  rendered.unmount();
  vi.mocked(api.clipPlan).mockResolvedValue({
    ...plan(),
    source: { ...plan().source, media_type: "audio" },
    source_status: "audio_only",
  });
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  await screen.findByRole("list", { name: "精彩候选" });
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "查看生成说明" })).toBeNull();
});
it("F2 recovers an ACK loss using the exact frozen quote and selection after reload", async () => {
  const { store } = exportSetup();
  vi.mocked(api.confirmQuote).mockRejectedValueOnce(
    new ApiError("REQUEST_TIMEOUT", "lost ACK"),
  );
  const rendered = page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  const dialog = await selectFirstAndQuote();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认并生成" }));
  await waitFor(() =>
    expect(store.findContent("demo_one")?.pendingClipExport?.quoteId).toBe(
      "export-quote",
    ),
  );
  await screen.findByRole("button", { name: "继续检查视频生成" });
  rendered.unmount();
  store.stopAll();
  const restored = createWorkspace();
  restored.switchUser("clips-user");
  stores.push(restored);
  page(restored, "/video/demo_one?tab=clips&clip_plan=plan-one");
  fireEvent.click(
    await screen.findByRole("button", { name: "继续检查视频生成" }),
  );
  await waitFor(() => expect(restored.tasks).toHaveLength(1));
  expect(restored.tasks[0].clipPlanId).toBe("plan-one");
  expect(restored.findContent("demo_one")?.pendingClipExport).toBeUndefined();
  expect(api.clipExportQuote).toHaveBeenCalledTimes(1);
  expect(api.confirmQuote).toHaveBeenNthCalledWith(2, "export-quote");
});
it.each(["success", "definite", "uncertain"] as const)(
  "F2 settles export confirmation after unmount: %s",
  async (outcome) => {
    const pending = deferred<{ task_id: string; status: "queued" }>();
    const { store } = exportSetup();
    vi.mocked(api.confirmQuote).mockReturnValue(pending.promise);
    const rendered = page(
      store,
      "/video/demo_one?tab=clips&clip_plan=plan-one",
    );
    const dialog = await selectFirstAndQuote();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认并生成" }));
    rendered.unmount();
    await act(async () => {
      if (outcome === "success")
        pending.resolve({ task_id: "export-job", status: "queued" });
      else
        pending.reject(
          new ApiError(
            outcome === "definite" ? "QUOTE_EXPIRED" : "REQUEST_TIMEOUT",
            "test",
            "",
            outcome === "definite" ? 409 : undefined,
          ),
        );
    });
    expect(store.tasks).toHaveLength(outcome === "success" ? 1 : 0);
    expect(store.findContent("demo_one")?.pendingClipExport?.quoteId).toBe(
      outcome === "uncertain" ? "export-quote" : undefined,
    );
  },
);
it("F2 replaces an invalidated export quote only after an explicit refresh and a second consent", async () => {
  const { store } = exportSetup();
  vi.mocked(api.confirmQuote).mockRejectedValueOnce(
    new ApiError("CLIP_REQUOTE_REQUIRED", "requote", "", 409),
  );
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  const dialog = await selectFirstAndQuote();
  fireEvent.click(within(dialog).getByRole("button", { name: "确认并生成" }));
  await waitFor(() =>
    expect(store.findContent("demo_one")?.pendingClipExport).toBeUndefined(),
  );
  fireEvent.click(await screen.findByRole("button", { name: "重新获取报价" }));
  await waitFor(() => expect(api.clipExportQuote).toHaveBeenCalledTimes(2));
  expect(api.confirmQuote).toHaveBeenCalledTimes(1);
  expect(
    (
      screen.getByRole("checkbox", {
        name: "选择片段：完整观点 plan-one",
      }) as HTMLInputElement
    ).checked,
  ).toBe(true);
});
it("F2 restores item states by candidate ID and distinguishes missing files from expired files", async () => {
  const { store } = exportSetup();
  const value = exported();
  value.items[0].download_available = false; // candidate-2, despite response order
  value.items[1].expires_at = "2000-01-01T00:00:00Z";
  vi.mocked(api.clipExport).mockResolvedValue(value);
  page(store, "/video/demo_one?tab=clips&clip_export=export-one");
  const files = await screen.findByRole("list", { name: "已生成的视频片段" });
  const items = within(files).getAllByRole("listitem");
  expect(
    within(items[0]).getByRole("heading", { name: "另一处观点" }),
  ).toBeTruthy();
  expect(within(items[0]).getByText(/文件不可用/)).toBeTruthy();
  expect(within(items[1]).getByText(/已到期/)).toBeTruthy();
  expect(
    within(files)
      .getAllByRole("button")
      .every((button) => (button as HTMLButtonElement).disabled),
  ).toBe(true);
});
it("F2 refreshes availability before obtaining one fresh native-download grant", async () => {
  const { store } = exportSetup();
  const access = vi
    .spyOn(clientApi, "resourceAccessUrl")
    .mockResolvedValue("https://example.com/download?access_token=short-lived");
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(() => {});
  page(store, "/video/demo_one?tab=clips&clip_export=export-one");
  fireEvent.click(
    await screen.findByRole("button", { name: "下载视频：完整观点 plan-one" }),
  );
  await screen.findByText("已请求下载，请查看浏览器下载列表。");
  expect(access).toHaveBeenCalledWith(
    "/files/export-one-file-0/download",
    expect.any(AbortSignal),
  );
  expect(click).toHaveBeenCalledTimes(1);
  expect(localStorage.getItem("videohub.videos.v1:clips-user")).not.toContain(
    "short-lived",
  );
  expect(localStorage.getItem("videohub.videos.v1:clips-user")).not.toContain(
    "download_url",
  );
});
it("F2 stops before obtaining a grant if the file disappeared after rendering the button", async () => {
  const { store } = exportSetup();
  const access = vi
    .spyOn(clientApi, "resourceAccessUrl")
    .mockResolvedValue("unused");
  page(store, "/video/demo_one?tab=clips&clip_export=export-one");
  const button = await screen.findByRole("button", {
    name: "下载视频：完整观点 plan-one",
  });
  vi.mocked(api.clipExport).mockResolvedValue({
    ...exported(),
    items: exported().items.map((item) => ({
      ...item,
      download_available: false,
    })),
  });
  fireEvent.click(button);
  await screen.findByText("文件已到期或不可用，请重新获取生成报价。");
  expect(access).not.toHaveBeenCalled();
});
it("F2 ignores a download grant arriving after an account switch", async () => {
  const pending = deferred<string>();
  const { store } = exportSetup();
  vi.spyOn(clientApi, "resourceAccessUrl").mockReturnValue(pending.promise);
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(() => {});
  page(store, "/video/demo_one?tab=clips&clip_export=export-one");
  fireEvent.click(
    await screen.findByRole("button", { name: "下载视频：完整观点 plan-one" }),
  );
  await waitFor(() => expect(clientApi.resourceAccessUrl).toHaveBeenCalled());
  act(() => {
    login("other-user");
    store.switchUser("other-user");
  });
  await act(async () =>
    pending.resolve("https://example.com/private-download"),
  );
  expect(click).not.toHaveBeenCalled();
});
it("F2 retains delivered files while a newer export fails without a complete batch resource", async () => {
  const { store, wrapper } = exportSetup();
  store.rememberClipExport("demo_one", "plan-one", "export-one");
  const { result } = renderHook(
    () => useClipDelivery(plan(), "demo_one", undefined, true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.delivery?.id).toBe("export-one"));
  act(() =>
    store.adoptClipTask(
      {
        ...exportSnapshot(),
        task_id: "failed-export",
        status: "failed",
        result_payload: { file_ids: ["partial-file"] },
      },
      "demo_one",
    ),
  );
  act(() => result.current.reload());
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.delivery?.id).toBe("export-one");
  expect(
    result.current.delivery?.items.some(
      (item) => item.file_id === "partial-file",
    ),
  ).toBe(false);
});
it("F2 accepts a newly completed request that reuses an older delivery while explicit links keep their batch", async () => {
  const { store, wrapper } = exportSetup();
  store.adoptClipTask(
    exportSnapshot("export-two", "second-export"),
    "demo_one",
  );
  const { result } = renderHook(
    () => useClipDelivery(plan(), "demo_one", exported("export-two"), true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.delivery?.id).toBe("export-two"));
  act(() =>
    store.adoptClipTask(
      {
        ...exportSnapshot("export-one", "third-cache"),
        created_at: "2026-11-03T12:00:00Z",
      },
      "demo_one",
    ),
  );
  await waitFor(() => expect(result.current.delivery?.id).toBe("export-one"));
  expect(result.current.error).toBeNull();
});
it("F2 rejects foreign candidates and incomplete batches without exposing partial file buttons", async () => {
  const { store } = exportSetup();
  const invalid = exported();
  invalid.items[0].candidate_id = "foreign-candidate";
  vi.mocked(api.clipExport).mockResolvedValue(invalid);
  page(store, "/video/demo_one?tab=clips&clip_export=export-one");
  await screen.findByText("切片成品数据不完整或与当前方案不一致，请重新加载");
  expect(screen.queryByRole("list", { name: "已生成的视频片段" })).toBeNull();
});
it("F2 preserves a new pending quote and fences old-account export confirmations", async () => {
  const pending = deferred<{ task_id: string; status: "queued" }>();
  const { store } = exportSetup();
  vi.mocked(api.confirmQuote).mockReturnValue(pending.promise);
  const input = {
    plan_id: "plan-one",
    revision: 1,
    candidate_ids: ["candidate-1"],
    profile: "video_720_mp4" as const,
  };
  const promise = store
    .confirmClipExport(store.findContent("demo_one")!, "old", input)
    .catch((error: ApiError) => error.code);
  store.switchUser(null);
  store.switchUser("clips-user");
  store.setClipExportConfirmation("demo_one", { ...input, quoteId: "new" });
  pending.resolve({ task_id: "old-account-export", status: "queued" });
  expect(await promise).toBe("AUTH_CHANGED");
  expect(store.findContent("demo_one")?.pendingClipExport?.quoteId).toBe("new");
  expect(store.tasks).toHaveLength(0);
});

it("F2 clears selection on an accepted plan change without changing the previous draft's identity", async () => {
  const { store } = exportSetup();
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  fireEvent.click(
    await screen.findByRole("checkbox", {
      name: "选择片段：完整观点 plan-one",
    }),
  );
  act(() => store.adoptClipTask(snapshot("completed", "plan-two"), "demo_one"));
  fireEvent.click(await screen.findByRole("button", { name: "查看新候选" }));
  const checkbox = await screen.findByRole("checkbox", {
    name: "选择片段：完整观点 plan-two",
  });
  expect((checkbox as HTMLInputElement).checked).toBe(false);
  expect(store.findContent("demo_one")?.clipSelection?.plan_id).toBe(
    "plan-one",
  );
  expect(
    (screen.getByRole("button", { name: "查看生成说明" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
});

it("F2 rejects a quote for the wrong operation without submitting it", async () => {
  const { store } = exportSetup();
  vi.mocked(api.clipExportQuote).mockResolvedValue(proposal);
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  fireEvent.click(
    await screen.findByRole("checkbox", {
      name: "选择片段：完整观点 plan-one",
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "查看生成说明" }));
  await screen.findByText("生成报价与操作不一致，请重新获取");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(api.confirmQuote).not.toHaveBeenCalled();
});

it("F2 discards a quote arriving after the user changes accounts", async () => {
  const pending = deferred<AllowanceQuote>();
  const { store } = exportSetup();
  vi.mocked(api.clipExportQuote).mockReturnValue(pending.promise);
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  fireEvent.click(
    await screen.findByRole("checkbox", {
      name: "选择片段：完整观点 plan-one",
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "查看生成说明" }));
  act(() => {
    login("other-user");
    store.switchUser("other-user");
  });
  await act(async () => pending.resolve(exportProposal));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(api.confirmQuote).not.toHaveBeenCalled();
  expect(store.findContent("demo_one")?.pendingClipExport).toBeUndefined();
});

it("F2 restores a read-only export when the creation flag is disabled without probing quote capabilities", async () => {
  const { store } = exportSetup();
  vi.stubEnv("VITE_CLIPS_ENABLED", "false");
  page(store, "/video/demo_one?tab=clips&clip_export=export-one");
  await screen.findByRole("list", { name: "已生成的视频片段" });
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(api.clipExportQuote).not.toHaveBeenCalled();
  expect(api.confirmQuote).not.toHaveBeenCalled();
});

it("F2 ignores malformed local selection, pending confirmation, and batch references", async () => {
  const { store } = exportSetup();
  const videos = JSON.parse(
    localStorage.getItem("videohub.videos.v1:clips-user")!,
  );
  videos[0].clipSelection = {
    plan_id: "plan-one",
    revision: 1,
    candidate_ids: "broken",
  };
  videos[0].pendingClipExport = { quoteId: "broken", candidate_ids: null };
  videos[0].clipExports = "broken";
  localStorage.setItem("videohub.videos.v1:clips-user", JSON.stringify(videos));
  store.stopAll();
  const restored = createWorkspace();
  restored.switchUser("clips-user");
  stores.push(restored);
  page(restored, "/video/demo_one?tab=clips&clip_plan=plan-one");
  await screen.findByRole("list", { name: "精彩候选" });
  expect(
    screen
      .getAllByRole("checkbox")
      .every((item) => !(item as HTMLInputElement).checked),
  ).toBe(true);
  expect(restored.findContent("demo_one")?.pendingClipExport).toBeUndefined();
});

it("F2 honours an explicit older delivery deep link over already-loaded newer history", async () => {
  const { store, wrapper } = exportSetup();
  store.adoptClipTask(
    exportSnapshot("export-two", "second-export"),
    "demo_one",
  );
  const { result } = renderHook(
    () => useClipDelivery(plan(), "demo_one", exported("export-one"), true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.delivery?.id).toBe("export-one"));
  act(() => result.current.reload());
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.delivery?.id).toBe("export-one");
});

it("F2 does not clear a later confirmation on a delayed success in the same account", async () => {
  const { store } = exportSetup();
  const pending = deferred<{ task_id: string; status: "queued" }>();
  vi.mocked(api.confirmQuote).mockReturnValue(pending.promise);
  const input = {
    plan_id: "plan-one",
    revision: 1,
    candidate_ids: ["candidate-1"],
    profile: "video_720_mp4" as const,
  };
  const promise = store.confirmClipExport(
    store.findContent("demo_one")!,
    "old",
    input,
  );
  store.setClipExportConfirmation("demo_one", { ...input, quoteId: "new" });
  pending.resolve({ task_id: "export-job", status: "queued" });
  await promise;
  expect(store.findContent("demo_one")?.pendingClipExport?.quoteId).toBe("new");
});

it("F2 does not roll back a new batch when an old download availability check arrives late", async () => {
  const { store, wrapper } = exportSetup();
  const { result } = renderHook(
    () => useClipDelivery(plan(), "demo_one", exported("export-one"), true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.delivery?.id).toBe("export-one"));
  await waitFor(() => expect(result.current.loading).toBe(false));
  const pending = deferred<ClipExport>();
  vi.mocked(api.clipExport).mockImplementation(async (id) =>
    id === "export-one" ? pending.promise : exported(id),
  );
  const check = result.current
    .check(new AbortController().signal)
    .catch((error: ApiError) => error.code);
  act(() =>
    store.adoptClipTask(
      exportSnapshot("export-two", "second-export"),
      "demo_one",
    ),
  );
  await waitFor(() => expect(result.current.delivery?.id).toBe("export-two"));
  await act(async () => pending.resolve(exported("export-one")));
  expect(await check).toBe("AUTH_CHANGED");
  expect(result.current.delivery?.id).toBe("export-two");
});

it("F2 does not fetch a delivery again merely because its reference was persisted", async () => {
  const { store, wrapper } = exportSetup();
  store.adoptClipTask(exportSnapshot(), "demo_one");
  const { result } = renderHook(
    () => useClipDelivery(plan(), "demo_one", undefined, true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.delivery?.id).toBe("export-one"));
  await act(async () => {});
  expect(api.clipExport).toHaveBeenCalledTimes(1);
  act(() => result.current.reload());
  await waitFor(() => expect(api.clipExport).toHaveBeenCalledTimes(2));
});

it("F2 restores focus to the generation trigger after an asynchronous quote dialog closes", async () => {
  const { store } = exportSetup();
  const pending = deferred<AllowanceQuote>();
  vi.mocked(api.clipExportQuote).mockReturnValue(pending.promise);
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  const checkbox = await screen.findByRole("checkbox", {
    name: "选择片段：完整观点 plan-one",
  });
  fireEvent.click(checkbox);
  const trigger = screen.getByRole("button", { name: "查看生成说明" });
  trigger.focus();
  fireEvent.click(trigger);
  checkbox.focus();
  await act(async () => pending.resolve(exportProposal));
  fireEvent.click(
    within(
      await screen.findByRole("dialog", { name: "确认生成视频" }),
    ).getByRole("button", { name: "暂不开始" }),
  );
  expect(document.activeElement).toBe(trigger);
});

it("F2 keeps recovery for an old pending export available when the current analysis has no highlights", async () => {
  const { store } = exportSetup();
  store.setClipExportConfirmation("demo_one", {
    plan_id: "plan-one",
    revision: 1,
    candidate_ids: ["candidate-1"],
    profile: "video_720_mp4",
    quoteId: "old-export-quote",
  });
  vi.mocked(api.clipPlan).mockResolvedValue({
    ...plan("plan-two"),
    outcome: "no_highlights",
    candidates: [],
    reason: "没有合适内容",
  });
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-two");
  await screen.findByRole("heading", { name: "没有找到符合当前条件的片段" });
  fireEvent.click(
    await screen.findByRole("button", { name: "继续检查视频生成" }),
  );
  await waitFor(() => expect(store.tasks).toHaveLength(1));
  expect(store.tasks[0].clipPlanId).toBe("plan-one");
  expect(api.confirmQuote).toHaveBeenCalledWith("old-export-quote");
  expect(api.clipExportQuote).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "查看生成说明" })).toBeNull();
});

it("defaults to AI duration without seconds inputs and sends an unambiguous semantic strategy", async () => {
  const { store } = setup();
  page(store);
  const mode = await screen.findByRole("combobox", { name: /片段时长策略/ });
  expect((mode as HTMLSelectElement).value).toBe("auto");
  expect(
    screen.queryByRole("spinbutton", { name: "最小时长（秒）" }),
  ).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查看分析额度" }));
  await screen.findByRole("dialog");
  expect(api.clipPlanQuote).toHaveBeenCalledWith(
    expect.objectContaining({
      strategy: {
        count: 3,
        duration_mode: "auto",
        min_seconds: null,
        max_seconds: null,
        focus: "",
      },
    }),
    expect.any(AbortSignal),
  );
  expect(api.confirmQuote).not.toHaveBeenCalled();
});

it("allows optional multi-minute manual ranges and invalidates them when returning to AI duration", async () => {
  const { store } = setup();
  const pending = deferred<AllowanceQuote>();
  vi.mocked(api.clipPlanQuote).mockReturnValueOnce(pending.promise);
  page(store);
  const mode = await screen.findByRole("combobox", { name: /片段时长策略/ });
  fireEvent.change(mode, { target: { value: "manual" } });
  fireEvent.change(screen.getByRole("spinbutton", { name: "最大时长（秒）" }), {
    target: { value: "240" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查看分析额度" }));
  expect(api.clipPlanQuote).toHaveBeenLastCalledWith(
    expect.objectContaining({
      strategy: expect.objectContaining({
        duration_mode: "manual",
        min_seconds: 30,
        max_seconds: 240,
      }),
    }),
    expect.any(AbortSignal),
  );
  fireEvent.change(mode, { target: { value: "auto" } });
  await act(async () => pending.resolve(proposal));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(
    screen.queryByRole("spinbutton", { name: "最大时长（秒）" }),
  ).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查看分析额度" }));
  await screen.findByRole("dialog");
  expect(api.clipPlanQuote).toHaveBeenLastCalledWith(
    expect.objectContaining({
      strategy: expect.objectContaining({
        duration_mode: "auto",
        min_seconds: null,
        max_seconds: null,
      }),
    }),
    expect.any(AbortSignal),
  );
});

it("reads legacy plans unchanged while making re-analysis default to AI duration", async () => {
  const { store } = setup();
  const legacy = plan();
  vi.mocked(api.clipPlan).mockResolvedValue(legacy);
  page(store, "/video/demo_one?tab=clips&clip_plan=plan-one");
  fireEvent.click(await screen.findByRole("button", { name: "调整查找条件" }));
  const mode = await screen.findByRole("combobox", { name: /片段时长策略/ });
  expect((mode as HTMLSelectElement).value).toBe("auto");
  expect(legacy.spec.strategy.duration_mode).toBeUndefined();
  expect(legacy.spec.strategy.max_seconds).toBe(90);
});
