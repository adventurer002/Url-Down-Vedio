// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { api } from "../api";
import { ApiError } from "../api/client";
import { authStore } from "../stores/auth";
import { billingStore } from "../stores/billing";
import { WorkspaceProvider } from "../hooks/useWorkspace";
import { createWorkspace, type SavedVideo } from "../stores/workspace";
import VideoQuestions, { originalLink } from "../components/VideoQuestions";
import SaveToLibrary from "../components/SaveToLibrary";
import LibraryView from "../views/LibraryView";
import { quote, allowances } from "./billingFixtures";
import type { LibraryDetail, VideoAnswer } from "../types/knowledge";
vi.mock("../lib/taskTracker", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createTaskTracker: vi.fn(() => ({ stop: vi.fn() })),
}));
const user = {
  id: "knowledge-a",
  nickname: "用户",
  role: "user",
  status: "active",
  is_subscribed: true,
} as const;
const video: SavedVideo = {
  key: "bilibili_one",
  url: "https://www.bilibili.com/video/one",
  metadata: {
    platform: "bilibili",
    platform_video_id: "one",
    title: "缓存与事务",
    duration: 120,
    thumbnail: null,
    uploader: null,
    has_subtitles: true,
    subtitle_languages: ["zh"],
    download_options: [],
  },
};
const detail: LibraryDetail = {
  id: "saved-1",
  transcript_id: "transcript-1",
  result_id: null,
  title: "缓存与事务",
  video: {
    id: "v1",
    platform: "bilibili",
    platform_video_id: "one",
    source_url: video.url,
    title: "缓存与事务",
    duration: 120,
    thumbnail: null,
    uploader: null,
    subtitle_languages: ["zh"],
  },
  source_type: "asr",
  tags: ["后端"],
  excerpt: "缓存保证读取效率",
  content_text: "缓存键需要包含版本。事务保证结算一次。",
  summary_text: "模型版本属于缓存键。",
  created_at: "2026-10-24T00:00:00Z",
  updated_at: "2026-10-24T00:00:00Z",
};
const answer: VideoAnswer = {
  id: "answer-1",
  task_id: "job-1",
  transcript_id: "transcript-1",
  question: "缓存键如何设置？",
  answer: "缓存键应包含模型版本。",
  citations: [
    {
      id: 1,
      start: 40,
      end: 160,
      quote: "缓存键需要包含版本。",
      approximate: true,
    },
  ],
  model: "test",
  prompt_version: "question-v1",
  created_at: "2026-10-24T00:00:00Z",
};
function login(id: string = user.id) {
  authStore.accept({
    status: "authenticated",
    access_token: "test",
    user: { ...user, id },
  });
}
function mount(child: React.ReactNode, path = "/") {
  const store = createWorkspace();
  return {
    ...render(
      <MemoryRouter initialEntries={[path]}>
        <WorkspaceProvider store={store}>{child}</WorkspaceProvider>
      </MemoryRouter>,
    ),
    store,
  };
}
beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => data.get(key) || null,
    setItem: (key: string, value: string) => data.set(key, value),
    removeItem: (key: string) => data.delete(key),
  });
  authStore.clear();
  authStore.close();
  billingStore.setUser(null);
  login();
  vi.spyOn(api, "answers").mockResolvedValue({ items: [] });
  vi.spyOn(api, "library").mockResolvedValue({
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
  });
  vi.spyOn(api, "libraryItem").mockResolvedValue(detail);
  vi.spyOn(api, "allowances").mockResolvedValue(allowances);
  vi.spyOn(api, "questionQuote").mockResolvedValue({
    ...quote,
    task_type: "ai_question",
    maximum: { ...quote.maximum, ai_generations: 1 },
  });
  vi.spyOn(api, "confirmQuote").mockResolvedValue({
    task_id: "job-1",
    status: "queued",
  });
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
});
afterEach(() => {
  cleanup();
  authStore.clear();
  authStore.close();
  billingStore.setUser(null);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
it("shows a quote before asking, cancels without creating a task, and retries the same confirmation", async () => {
  const { store } = mount(
    <VideoQuestions video={video} transcriptId="transcript-1" />,
  );
  fireEvent.change(screen.getByLabelText("你想进一步了解什么？"), {
    target: { value: "缓存键怎么设置？" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认额度并提问" }));
  let modal = await screen.findByRole("dialog", { name: "确认本次额度" });
  expect(api.confirmQuote).not.toHaveBeenCalled();
  fireEvent.click(within(modal).getByRole("button", { name: "暂不开始" }));
  expect(store.tasks).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "确认额度并提问" }));
  modal = await screen.findByRole("dialog", { name: "确认本次额度" });
  vi.mocked(api.confirmQuote).mockRejectedValueOnce(
    new ApiError("NETWORK_ERROR", "确认失败"),
  );
  fireEvent.click(within(modal).getByRole("button", { name: "确认并开始" }));
  await screen.findByText("确认失败");
  fireEvent.click(within(modal).getByRole("button", { name: "确认并开始" }));
  await waitFor(() => expect(store.tasks).toHaveLength(1));
  expect(api.questionQuote).toHaveBeenCalledWith(
    "transcript-1",
    "缓存键怎么设置？",
    expect.any(AbortSignal),
  );
  expect(vi.mocked(api.confirmQuote).mock.calls).toEqual([
    ["quote-1"],
    ["quote-1"],
  ]);
});
it("renders exact citations, approximate timestamps and a source link", async () => {
  vi.mocked(api.answers).mockResolvedValue({ items: [answer] });
  const jump = vi.fn();
  mount(
    <VideoQuestions video={video} transcriptId="transcript-1" onJump={jump} />,
  );
  await screen.findByText("缓存键应包含模型版本。");
  fireEvent.click(screen.getByText(/依据 1 · 约/));
  expect(screen.getByText("缓存键需要包含版本。")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "查看对应文稿" }));
  expect(jump).toHaveBeenCalledWith(answer.citations[0]);
  expect(
    screen.getByRole("link", { name: "打开原视频" }).getAttribute("href"),
  ).toContain("t=40");
  expect(originalLink("javascript:alert(1)", 4)).toBeUndefined();
});
it("saves tags with an owned transcript and never creates an AI task", async () => {
  vi.spyOn(api, "saveVideo").mockResolvedValue(detail);
  mount(<SaveToLibrary transcriptId="transcript-1" />);
  await waitFor(() =>
    expect(
      (
        screen.getByRole("button", {
          name: "收藏到知识库",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: "收藏到知识库" }));
  fireEvent.change(screen.getByLabelText("标签"), {
    target: { value: "后端，架构" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存收藏" }));
  await screen.findByRole("button", { name: "已收藏 · 编辑标签" });
  expect(api.saveVideo).toHaveBeenCalledWith("transcript-1", undefined, [
    "后端",
    "架构",
  ]);
  expect(api.confirmQuote).not.toHaveBeenCalled();
});
it("searches owned library text, filters tags and reads a saved result without generation", async () => {
  vi.mocked(api.library).mockResolvedValue({
    items: [detail],
    total: 1,
    limit: 20,
    offset: 0,
  });
  mount(<LibraryView />);
  fireEvent.click(await screen.findByRole("button", { name: "缓存与事务" }));
  expect(await screen.findByText("模型版本属于缓存键。")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("搜索知识库"), {
    target: { value: "事务" },
  });
  fireEvent.click(screen.getByRole("button", { name: "搜索" }));
  await waitFor(() =>
    expect(api.library).toHaveBeenCalledWith(
      "事务",
      "",
      0,
      expect.any(AbortSignal),
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "#后端" }));
  await waitFor(() =>
    expect(api.library).toHaveBeenCalledWith(
      "事务",
      "后端",
      0,
      expect.any(AbortSignal),
    ),
  );
  expect(api.confirmQuote).not.toHaveBeenCalled();
});
it("removes a saved item and shows the empty state", async () => {
  vi.mocked(api.library).mockResolvedValue({
    items: [detail],
    total: 1,
    limit: 20,
    offset: 0,
  });
  vi.spyOn(api, "unsaveVideo").mockResolvedValue({ removed: true });
  mount(<LibraryView />);
  fireEvent.click(await screen.findByRole("button", { name: "缓存与事务" }));
  await screen.findByRole("button", { name: "移出收藏" });
  vi.mocked(api.library).mockResolvedValue({
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
  });
  fireEvent.click(screen.getByRole("button", { name: "移出收藏" }));
  expect(await screen.findByText("从第一条收藏开始")).toBeTruthy();
  expect(api.unsaveVideo).toHaveBeenCalledWith("saved-1");
});
it("does not show a previous account’s pending library response after switching users", async () => {
  let finish!: (value: {
    items: LibraryDetail[];
    total: number;
    limit: number;
    offset: number;
  }) => void;
  vi.mocked(api.library).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  mount(<LibraryView />);
  act(() => login("knowledge-b"));
  await act(async () => {
    finish({ items: [detail], total: 1, limit: 20, offset: 0 });
  });
  expect(screen.queryByRole("button", { name: "缓存与事务" })).toBeNull();
});

it("opens an older purchased answer directly even when the recent list is empty", async () => {
  vi.spyOn(api, "answer").mockResolvedValue(answer);
  mount(
    <VideoQuestions video={video} transcriptId="transcript-1" />,
    "/?tab=questions&answer=answer-1",
  );
  expect(await screen.findByText("缓存键应包含模型版本。")).toBeTruthy();
  expect(api.answer).toHaveBeenCalledWith("answer-1", expect.any(AbortSignal));
  expect(api.confirmQuote).not.toHaveBeenCalled();
});
