// @vitest-environment jsdom
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
import AdminView from "../views/AdminView";
import { WorkspaceProvider } from "../hooks/useWorkspace";
import { authStore } from "../stores/auth";
import { adminApi } from "../admin/api";
import { ApiError, client } from "../api/client";
import { readTaskFilters } from "../admin/list";
import { DateTimeCell } from "../admin/ui";
import { task } from "./adminFixtures";

export const userId = "00000000-0000-4000-8000-000000000001";
export const taskId = "00000000-0000-4000-8000-000000000002";
const fullTask = { ...task, task_id: taskId, user_id: userId };
function mount(path = "/admin/tasks") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <WorkspaceProvider>
        <AdminView />
      </WorkspaceProvider>
    </MemoryRouter>,
  );
}
beforeEach(() => {
  authStore.clear();
  authStore.accept({
    status: "authenticated",
    access_token: "local-list-test",
    user: {
      id: "admin-list-test",
      nickname: "管理员",
      role: "admin",
      status: "active",
      is_subscribed: true,
    },
  });
  vi.spyOn(adminApi, "tasks").mockResolvedValue({
    items: [fullTask],
    limit: 20,
    offset: 0,
    total: 1,
  });
  vi.spyOn(adminApi, "task").mockResolvedValue(fullTask);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  authStore.clear();
});
it("submits task filters with explicit Beijing timezone and resets pagination", async () => {
  mount("/admin/tasks?offset=40");
  await screen.findByText(task.video!.title);
  fireEvent.change(screen.getByLabelText("任务状态"), {
    target: { value: "failed" },
  });
  fireEvent.click(screen.getByText("更多筛选"));
  fireEvent.change(screen.getByLabelText("用户编号"), {
    target: { value: userId },
  });
  fireEvent.change(screen.getByLabelText("开始时间（北京时间）"), {
    target: { value: "2026-10-26T00:00" },
  });
  fireEvent.change(screen.getByLabelText("结束时间（北京时间）"), {
    target: { value: "2026-10-27T00:00" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() =>
    expect(adminApi.tasks).toHaveBeenLastCalledWith(
      expect.objectContaining({
        offset: 0,
        status: "failed",
        user_id: userId,
        since: "2026-10-25T16:00:00.000Z",
        until: "2026-10-26T16:00:00.000Z",
      }),
      expect.any(AbortSignal),
    ),
  );
});
it("rejects invalid task UUID and reversed dates before sending a list request", async () => {
  mount("/admin/tasks?user_id=broken");
  expect(screen.getByRole("alert").textContent).toContain("完整 UUID");
  expect(adminApi.tasks).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "重置" }));
  await screen.findByText(task.video!.title);
  vi.mocked(adminApi.tasks).mockClear();
  fireEvent.change(screen.getByLabelText("开始时间（北京时间）"), {
    target: { value: "2026-10-27T00:00" },
  });
  fireEvent.change(screen.getByLabelText("结束时间（北京时间）"), {
    target: { value: "2026-10-26T00:00" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  expect(await screen.findByText("开始时间必须早于结束时间。")).toBeTruthy();
  expect(adminApi.tasks).not.toHaveBeenCalled();
});
it("retains task filters and page when returning from a detail", async () => {
  vi.mocked(adminApi.tasks).mockResolvedValue({
    items: [fullTask],
    limit: 20,
    offset: 20,
    total: 41,
  });
  mount("/admin/tasks?status=completed&offset=20");
  fireEvent.click(
    await screen.findByRole("link", { name: "查看任务 00000000" }),
  );
  expect(await screen.findByRole("heading", { name: "任务详情" })).toBeTruthy();
  await screen.findByText("9 秒");
  fireEvent.click(screen.getByRole("link", { name: "返回任务列表" }));
  expect(await screen.findByText(task.video!.title)).toBeTruthy();
  expect((screen.getByLabelText("任务状态") as HTMLSelectElement).value).toBe(
    "completed",
  );
  expect(screen.getByText("第 2 页")).toBeTruthy();
});
it("cancels filtered-out requests and cannot render their late result", async () => {
  let resolve!: (value: Awaited<ReturnType<typeof adminApi.tasks>>) => void;
  vi.mocked(adminApi.tasks).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  mount();
  await waitFor(() => expect(adminApi.tasks).toHaveBeenCalled());
  const signal = vi.mocked(adminApi.tasks).mock.calls[0]![1]!;
  fireEvent.change(screen.getByLabelText("任务状态"), {
    target: { value: "failed" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  expect(signal.aborted).toBe(true);
  await screen.findByText(task.video!.title);
  await act(async () =>
    resolve({
      items: [
        { ...fullTask, video: { ...fullTask.video!, title: "迟到旧任务" } },
      ],
      limit: 20,
      offset: 0,
      total: 1,
    }),
  );
  expect(screen.queryByText("迟到旧任务")).toBeNull();
});
it("shows list errors and request ID separately from a successful empty list", async () => {
  vi.mocked(adminApi.tasks).mockRejectedValueOnce(
    new ApiError("UNAVAILABLE", "任务查询暂不可用", "req-list"),
  );
  mount();
  expect(await screen.findByText("请求编号：req-list")).toBeTruthy();
  expect(screen.queryByText("暂无记录")).toBeNull();
  vi.mocked(adminApi.tasks).mockResolvedValue({
    items: [],
    limit: 20,
    offset: 0,
    total: 0,
  });
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  expect(await screen.findByText("暂无记录")).toBeTruthy();
});
it("serializes read-only task filters and forwards AbortSignal to Axios", async () => {
  vi.mocked(adminApi.tasks).mockRestore();
  const get = vi
    .spyOn(client, "get")
    .mockResolvedValue({ data: { items: [] } });
  const controller = new AbortController();
  await adminApi.tasks(
    {
      limit: 20,
      offset: 20,
      status: "failed",
      since: "2026-10-25T16:00:00.000Z",
    },
    controller.signal,
  );
  expect(get).toHaveBeenCalledWith(
    "/admin/tasks?limit=20&offset=20&status=failed&since=2026-10-25T16%3A00%3A00.000Z",
    { signal: controller.signal, timeout: 5000 },
  );
});
it("rejects calendar overflow rather than silently normalizing dates", () => {
  expect(
    readTaskFilters(new URLSearchParams({ since: "2026-02-30T00:00" })).error,
  ).toContain("有效的北京时间");
});

it("polls active tasks quickly and keeps checking for new tasks after completion", async () => {
  vi.useFakeTimers();
  try {
    vi.mocked(adminApi.tasks)
      .mockResolvedValueOnce({
        items: [{ ...fullTask, status: "running", progress: 40 }],
        limit: 20,
        offset: 0,
        total: 1,
      })
      .mockResolvedValue({ items: [fullTask], limit: 20, offset: 0, total: 1 });
    mount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(adminApi.tasks).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(adminApi.tasks).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(adminApi.tasks).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect(adminApi.tasks).toHaveBeenCalledTimes(3);
  } finally {
    cleanup();
    vi.useRealTimers();
  }
});

it("keeps an out-of-range page recoverable with the previous-page control", async () => {
  vi.mocked(adminApi.tasks).mockResolvedValue({
    items: [],
    total: 1,
    limit: 20,
    offset: 20,
  });
  mount("/admin/tasks?status=failed&offset=20");
  await screen.findByText("暂无记录");
  const previous = screen.getByRole("button", { name: "上一页" });
  expect(previous).toHaveProperty("disabled", false);
  fireEvent.click(previous);
  await waitFor(() =>
    expect(adminApi.tasks).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 0, status: "failed" }),
      expect.any(AbortSignal),
    ),
  );
});

it("keeps advanced filters as a draft when collapsed and includes them only on query", async () => {
  mount("/admin/tasks?offset=20");
  await screen.findByText(task.video!.title);
  const summary = screen.getByText("更多筛选");
  expect(summary.closest("details")).toHaveProperty("open", false);
  fireEvent.click(summary);
  fireEvent.change(screen.getByLabelText("用户编号"), {
    target: { value: userId },
  });
  fireEvent.click(summary);
  expect(summary.closest("details")).toHaveProperty("open", false);
  expect(adminApi.tasks).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() =>
    expect(adminApi.tasks).toHaveBeenLastCalledWith(
      expect.objectContaining({ user_id: userId, offset: 0 }),
      expect.any(AbortSignal),
    ),
  );
  expect(screen.getByText("已应用 1 项")).toBeTruthy();
  expect(screen.getByText("更多筛选").closest("details")).toHaveProperty(
    "open",
    true,
  );
});

it("opens active advanced filters from the URL and clears them even after collapse", async () => {
  mount(`/admin/tasks?user_id=${userId}&since=2026-10-26T00:00&offset=20`);
  await screen.findByText(task.video!.title);
  const summary = screen.getByText("更多筛选");
  expect(summary.closest("details")).toHaveProperty("open", true);
  expect(screen.getByText("已应用 2 项")).toBeTruthy();
  fireEvent.click(summary);
  fireEvent.click(screen.getByRole("button", { name: "重置" }));
  await waitFor(() =>
    expect(adminApi.tasks).toHaveBeenLastCalledWith(
      expect.objectContaining({ user_id: "", offset: 0 }),
      expect.any(AbortSignal),
    ),
  );
  expect(screen.queryByText(/已应用/)).toBeNull();
  expect(screen.getByLabelText("用户编号")).toHaveProperty("value", "");
  expect(screen.getByLabelText("开始时间（北京时间）")).toHaveProperty(
    "value",
    "",
  );
});

it("copies the complete task ID while preserving the abbreviated display and detail link", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  mount();
  const copy = await screen.findByRole("button", {
    name: `复制任务编号 ${taskId}`,
  });
  fireEvent.click(copy);
  await screen.findByText("任务编号已复制");
  expect(writeText).toHaveBeenCalledWith(taskId);
  expect(
    screen
      .getByRole("link", { name: "查看任务 00000000" })
      .getAttribute("href"),
  ).toContain(`/admin/tasks/${taskId}`);
  expect(adminApi.task).not.toHaveBeenCalled();
});

it("exposes a manual ID copy fallback when clipboard access is unavailable", async () => {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
  });
  mount();
  fireEvent.click(
    await screen.findByRole("button", { name: `复制任务编号 ${taskId}` }),
  );
  const feedback = await screen.findByText("复制失败，可手动复制：");
  expect(feedback.textContent).toContain(taskId);
  expect(screen.getByRole("link", { name: "查看任务 00000000" })).toBeTruthy();
});

it("splits date and time without changing Beijing timezone or losing the full timestamp", () => {
  render(<DateTimeCell value="2026-10-26T23:58:00Z" />);
  const value = screen.getByLabelText("2026/10/27 07:58");
  expect(value.getAttribute("datetime")).toBe("2026-10-26T23:58:00.000Z");
  expect(screen.getByText("2026/10/27")).toBeTruthy();
  expect(screen.getByText("07:58")).toBeTruthy();
});
