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
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import AdminView from "../views/AdminView";
import { WorkspaceProvider } from "../hooks/useWorkspace";
import { authStore } from "../stores/auth";
import { adminApi } from "../admin/api";
import { api } from "../api";
import { ApiError, client } from "../api/client";
import { overview, storage, task } from "./adminFixtures";
function accept(id = "admin-a", role = "admin", status = "active") {
  authStore.accept({
    status: "authenticated",
    access_token: `${id}-fixture-token`,
    user: { id, role, status, nickname: id, is_subscribed: true },
  });
}
function mount() {
  return render(
    <MemoryRouter initialEntries={["/admin"]}>
      <WorkspaceProvider>
        <AdminView />
      </WorkspaceProvider>
    </MemoryRouter>,
  );
}
beforeEach(() => {
  authStore.clear();
  authStore.close();
  vi.spyOn(adminApi, "overview").mockResolvedValue(structuredClone(overview));
  vi.spyOn(adminApi, "storage").mockResolvedValue(storage);
  vi.spyOn(adminApi, "recentTasks").mockResolvedValue({
    items: [task],
    total: 1,
    offset: 0,
    limit: 5,
  });
  vi.spyOn(adminApi, "task").mockResolvedValue(task);
  vi.spyOn(api, "ready").mockResolvedValue({
    status: "ready",
    database: "ready",
    redis: "ready",
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  authStore.clear();
  authStore.close();
});
it.each([
  ["anonymous", "", ""],
  ["ordinary", "user", "active"],
  ["disabled", "admin", "disabled"],
])("does not query admin data for %s visitors", async (id, role, status) => {
  if (role) accept(id, role, status);
  mount();
  expect(
    await screen.findByText("此页面仅供管理员访问。"),
  ).toBeTruthy();
  expect(adminApi.overview).not.toHaveBeenCalled();
  expect(adminApi.storage).not.toHaveBeenCalled();
  expect(adminApi.recentTasks).not.toHaveBeenCalled();
  expect(api.ready).not.toHaveBeenCalled();
});
it("waits for identity restoration and blocks on verification failure", async () => {
  let reject!: (error: unknown) => void;
  vi.spyOn(client, "get").mockImplementation(
    () =>
      new Promise((_, failure) => {
        reject = failure;
      }),
  );
  let restore!: Promise<void>;
  act(() => {
    restore = authStore.restore();
  });
  mount();
  expect(
    screen.getByRole("heading", { name: "正在验证管理员身份" }),
  ).toBeTruthy();
  expect(adminApi.overview).not.toHaveBeenCalled();
  await act(async () => {
    reject(new ApiError("NETWORK_ERROR", "身份服务暂不可用"));
    await restore;
  });
  expect(
    screen.getByRole("heading", { name: "暂时无法验证身份" }),
  ).toBeTruthy();
  expect(adminApi.overview).not.toHaveBeenCalled();
});
it("renders real metrics, unknown Worker liveness and actual storage quota", async () => {
  accept();
  mount();
  expect(await screen.findByLabelText("今日新建任务：32")).toHaveProperty(
    "textContent",
    "32",
  );
  expect(screen.getByLabelText("执行中任务：1")).toBeTruthy();
  expect(screen.getByLabelText("用户总数：128")).toBeTruthy();
  expect(screen.getByLabelText("有效期内会员：24")).toBeTruthy();
  expect(await screen.findByText("未监测")).toBeTruthy();
  expect(await screen.findByText("1.0 GiB / 5.0 GiB")).toBeTruthy();
  expect(screen.queryByText(/营收|P50|七日趋势/)).toBeNull();
});
it("preserves unknown metrics on failure and can reload them", async () => {
  vi.mocked(adminApi.overview).mockRejectedValueOnce(
    new ApiError("UNAVAILABLE", "统计暂不可用", "req-overview"),
  );
  accept();
  mount();
  expect(
    await screen.findByText("req-overview", { exact: false }),
  ).toBeTruthy();
  expect(screen.getByLabelText("今日新建任务：未知")).toHaveProperty(
    "textContent",
    "—",
  );
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  expect(await screen.findByLabelText("今日新建任务：32")).toBeTruthy();
});
it("does not turn an incomplete storage scan into a zero or percent", async () => {
  vi.mocked(adminApi.storage).mockResolvedValue({
    ...storage,
    status: "partial",
    used_bytes: null,
    scan_complete: false,
    observed_bytes: 2048,
  });
  accept();
  mount();
  expect(
    await screen.findByText("扫描未完成，已观察到 2.0 KiB，总占用未知。"),
  ).toBeTruthy();
  expect(
    screen.queryByRole("progressbar", { name: "存储配额占用" }),
  ).toBeNull();
});
it("shows empty state after a successful empty task page", async () => {
  vi.mocked(adminApi.recentTasks).mockResolvedValue({
    items: [],
    total: 0,
    limit: 5,
    offset: 0,
  });
  accept();
  mount();
  expect(await screen.findByText("暂无任务")).toBeTruthy();
});
it("refreshes all overview sources on demand", async () => {
  accept();
  mount();
  await screen.findByLabelText("今日新建任务：32");
  const button = screen.getByRole("button", { name: "刷新后台数据" });
  await waitFor(() => expect(button).toHaveProperty("disabled", false));
  fireEvent.click(button);
  await waitFor(() => expect(adminApi.overview).toHaveBeenCalledTimes(2));
  expect(adminApi.storage).toHaveBeenCalledTimes(2);
  expect(adminApi.recentTasks).toHaveBeenCalledTimes(2);
});
it("opens task metadata and restores trigger focus after Escape", async () => {
  accept();
  mount();
  const trigger = await screen.findByRole("button", {
    name: "查看任务 task-001",
  });
  trigger.focus();
  fireEvent.click(trigger);
  const dialog = await screen.findByRole("dialog", { name: "任务详情" });
  expect(await within(dialog).findByText("9 秒")).toBeTruthy();
  fireEvent.keyDown(dialog, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(document.activeElement).toBe(trigger);
});
it("dismisses mobile navigation with keyboard focus restoration", async () => {
  accept();
  mount();
  const trigger = screen.getByRole("button", { name: "打开管理导航" });
  trigger.focus();
  fireEvent.click(trigger);
  const dialog = await screen.findByRole("dialog", { name: "管理导航" });
  fireEvent.keyDown(dialog, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(document.activeElement).toBe(trigger);
});
it("cancels old requests and excludes late data after account switch", async () => {
  let resolve!: (value: typeof overview) => void;
  vi.mocked(adminApi.overview).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  accept();
  mount();
  await waitFor(() => expect(adminApi.overview).toHaveBeenCalled());
  const signal = vi.mocked(adminApi.overview).mock.calls[0]![0]!;
  act(() => accept("admin-b"));
  expect(signal.aborted).toBe(true);
  await screen.findByLabelText("用户总数：128");
  await act(async () => {
    resolve({ ...overview, users: { ...overview.users, total: 999 } });
  });
  expect(screen.queryByLabelText("用户总数：999")).toBeNull();
  act(() => authStore.clear());
  expect(screen.queryByLabelText("用户总数：128")).toBeNull();
});
it("stops admin reads and removes cached metrics after permission rejection", async () => {
  vi.mocked(adminApi.storage).mockRejectedValueOnce(
    new ApiError("FORBIDDEN", "管理员权限已失效", "", 403),
  );
  accept();
  mount();
  expect(
    await screen.findByRole("heading", { name: "管理权限需要重新确认" }),
  ).toBeTruthy();
  expect(screen.queryByLabelText("用户总数：128")).toBeNull();
  expect(screen.queryByRole("button", { name: "刷新后台数据" })).toBeNull();
});

it("does not let a late identity restoration overwrite a new login", async () => {
  let resolve!: (value: unknown) => void;
  vi.spyOn(client, "get").mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const pending = authStore.restore();
  authStore.open();
  accept("admin-new");
  resolve({ data: { id: "admin-old", role: "admin", status: "active" } });
  await pending;
  expect(authStore.getState().user?.id).toBe("admin-new");
  expect(authStore.getState().restoring).toBe(false);
  expect(authStore.getState().modalOpen).toBe(false);
});
