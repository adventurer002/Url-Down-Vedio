// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
const adminStyles = readFileSync(resolve("src/admin/admin.css"), "utf8");
const subscriptionStyles = readFileSync(
  resolve("src/subscription.css"),
  "utf8",
);
const sharedStyles = readFileSync(resolve("src/style.css"), "utf8");
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
import { adminApi, type StatusResult } from "../admin/api";
import { ApiError } from "../api/client";
import { adminUser, task } from "./adminFixtures";
const page = { items: [adminUser], total: 41, limit: 20, offset: 0 };
function mount(path = "/admin/users") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <WorkspaceProvider>
        <AdminView />
      </WorkspaceProvider>
    </MemoryRouter>,
  );
}
function styles(css: string) {
  const element = document.createElement("style");
  element.dataset.qaStyle = "true";
  element.textContent = css;
  document.head.append(element);
}
beforeEach(() => {
  sessionStorage.clear();
  authStore.clear();
  authStore.accept({
    status: "authenticated",
    access_token: "local-experience-fixture",
    user: {
      id: "experience-operator",
      nickname: "管理员",
      role: "admin",
      status: "active",
      is_subscribed: true,
    },
  });
  vi.spyOn(adminApi, "users").mockResolvedValue(page);
  vi.spyOn(adminApi, "user").mockResolvedValue({
    ...adminUser,
    status: "active",
    status_version: 0,
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  authStore.clear();
  sessionStorage.clear();
  document
    .querySelectorAll("[data-qa-style]")
    .forEach((element) => element.remove());
});
it("shows an animated loading region and skeleton instead of invented zero counts", async () => {
  let resolve!: (value: typeof page) => void;
  vi.mocked(adminApi.users).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  mount();
  const loading = screen.getByText("正在加载…").closest('[role="status"]')!;
  expect(loading.getAttribute("aria-busy")).toBe("true");
  expect(loading.querySelector(".admin-spinner")).toBeTruthy();
  expect(loading.querySelector(".admin-skeleton")).toBeTruthy();
  expect(screen.getByLabelText("匹配用户：未知")).toBeTruthy();
  expect(screen.queryByLabelText("匹配用户：0")).toBeNull();
  await act(async () => resolve(page));
  expect(await screen.findByLabelText("匹配用户：41")).toBeTruthy();
  expect(screen.queryByText("正在加载…")).toBeNull();
});
it("keeps current rows visible with explicit refresh feedback until the response arrives", async () => {
  mount();
  await screen.findByText(adminUser.nickname);
  let resolve!: (value: typeof page) => void;
  vi.mocked(adminApi.users).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const refresh = screen.getByRole("button", { name: "刷新后台数据" });
  fireEvent.click(refresh);
  await waitFor(() => expect(refresh.getAttribute("aria-busy")).toBe("true"));
  expect(refresh).toHaveProperty("disabled", true);
  expect(screen.getByText(adminUser.nickname)).toBeTruthy();
  expect(screen.getByText("正在加载后台数据…")).toBeTruthy();
  expect(document.querySelector(".admin-panel-progress")).toBeTruthy();
  await act(async () => resolve({ ...page, total: 12 }));
  await screen.findByLabelText("匹配用户：12");
  await waitFor(() => expect(refresh).toHaveProperty("disabled", false));
});
it("retains the last successful rows after refresh failure and labels them as previous results", async () => {
  mount();
  await screen.findByText(adminUser.nickname);
  vi.mocked(adminApi.users).mockRejectedValueOnce(
    new ApiError("UNAVAILABLE", "列表暂不可用", "req-refresh", 503),
  );
  fireEvent.click(screen.getByRole("button", { name: "刷新后台数据" }));
  expect(await screen.findByText("更新失败，显示上次结果")).toBeTruthy();
  expect(screen.getByText(adminUser.nickname)).toBeTruthy();
  expect(screen.getByLabelText("匹配用户：41")).toBeTruthy();
});
it("distinguishes matched totals from current-page membership counts", async () => {
  mount();
  await screen.findByLabelText("匹配用户：41");
  expect(screen.getByLabelText("本页会员：1")).toBeTruthy();
  expect(screen.getByLabelText("本页正常账号：0")).toBeTruthy();
  expect(screen.queryByLabelText("会员总数：1")).toBeNull();
});
it("quick filters preserve search and other conditions, reset the page and toggle off", async () => {
  mount("/admin/users?q=测试&member=true&offset=20");
  await screen.findByText(adminUser.nickname);
  fireEvent.click(screen.getByRole("button", { name: "停用账号" }));
  await waitFor(() =>
    expect(adminApi.users).toHaveBeenLastCalledWith(
      expect.objectContaining({
        q: "测试",
        member: "true",
        status: "disabled",
        offset: 0,
      }),
      expect.any(AbortSignal),
    ),
  );
  expect(
    screen
      .getByRole("button", { name: "停用账号" })
      .getAttribute("aria-pressed"),
  ).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "停用账号" }));
  await waitFor(() =>
    expect(adminApi.users).toHaveBeenLastCalledWith(
      expect.objectContaining({
        q: "测试",
        member: "true",
        status: "",
        offset: 0,
      }),
      expect.any(AbortSignal),
    ),
  );
});
it("recovers an empty filtered list by clearing the conditions", async () => {
  vi.mocked(adminApi.users).mockResolvedValue({ ...page, items: [], total: 0 });
  mount("/admin/users?q=不存在&offset=40");
  fireEvent.click(await screen.findByRole("button", { name: "清除筛选条件" }));
  await waitFor(() =>
    expect(adminApi.users).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: "", offset: 0 }),
      expect.any(AbortSignal),
    ),
  );
});
it("offers a first-page recovery when the current offset is outside the results", async () => {
  vi.mocked(adminApi.users).mockResolvedValue({
    ...page,
    items: [],
    total: 21,
  });
  mount("/admin/users?offset=200&member=true");
  fireEvent.click(await screen.findByRole("button", { name: "返回第一页" }));
  await waitFor(() =>
    expect(adminApi.users).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 0, member: "true" }),
      expect.any(AbortSignal),
    ),
  );
});
it("uses a fixed centered viewport for operation dialogs and restores trigger focus on Escape", async () => {
  styles(adminStyles);
  mount(`/admin/users/${adminUser.id}`);
  const trigger = await screen.findByRole("button", { name: "停用账号" });
  trigger.focus();
  fireEvent.click(trigger);
  const dialog = await screen.findByRole("dialog", { name: "账号状态操作" });
  const viewport = dialog.closest(".admin-modal-viewport")!;
  expect(getComputedStyle(viewport).position).toBe("fixed");
  expect(getComputedStyle(viewport).display).toBe("grid");
  expect(getComputedStyle(viewport).placeItems).toBe("center");
  expect(getComputedStyle(dialog).position).toBe("relative");
  fireEvent.keyDown(dialog, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(document.activeElement).toBe(trigger);
});
it("keeps native message dialogs centered despite a global margin reset, with internal overflow", () => {
  const subscription = subscriptionStyles;
  const shared = sharedStyles;
  styles(
    "* { margin: 0; }\n" +
      subscription.match(/\.billing-dialog\s*\{[^}]+\}/)![0] +
      "\n" +
      shared.match(/\.wechat-modal\s*\{[^}]+\}/)![0],
  );
  for (const className of ["billing-dialog", "wechat-modal"]) {
    const dialog = document.createElement("dialog");
    dialog.className = className;
    document.body.append(dialog);
    const computed = getComputedStyle(dialog);
    expect(computed.position).toBe("fixed");
    expect(computed.margin).toBe("auto");
    expect(computed.inset).toBe("0");
    expect(computed.overflowY).toBe("auto");
    dialog.remove();
  }
});
it("shows a spinner while an administrative write is pending without duplicate dispatch", async () => {
  let resolve!: (value: StatusResult) => void;
  const write = vi.spyOn(adminApi, "changeStatus").mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  mount(`/admin/users/${adminUser.id}`);
  fireEvent.click(await screen.findByRole("button", { name: "停用账号" }));
  const dialog = await screen.findByRole("dialog", { name: "账号状态操作" });
  fireEvent.change(await within(dialog).findByLabelText("操作原因"), {
    target: { value: "本地交互验收" },
  });
  fireEvent.click(within(dialog).getByRole("checkbox"));
  const submit = within(dialog).getByRole("button", { name: "确认停用" });
  await waitFor(() => expect(submit).toHaveProperty("disabled", false));
  fireEvent.click(submit);
  const pending = await within(dialog).findByText(/正在提交，请勿重复操作/);
  expect(pending.querySelector(".admin-spinner")).toBeTruthy();
  expect(write).toHaveBeenCalledTimes(1);
  expect(within(dialog).queryByRole("button", { name: "确认停用" })).toBeNull();
  await act(async () =>
    resolve({
      user_id: adminUser.id,
      status: "disabled",
      status_version: 1,
      audit_id: "fixture-audit",
      duplicate: false,
    }),
  );
  expect(await within(dialog).findByText(/停用已确认/)).toBeTruthy();
});
it("renders real task progress rather than a decorative completion graphic", async () => {
  const id = "00000000-0000-4000-8000-000000000040";
  vi.spyOn(adminApi, "task").mockResolvedValue({
    ...task,
    task_id: id,
    status: "processing",
    progress: 45,
  });
  mount(`/admin/tasks/${id}`);
  expect(
    await screen.findByRole("progressbar", { name: "任务执行进度" }),
  ).toHaveProperty("value", 45);
  expect(screen.getByText("当前进度 45%")).toBeTruthy();
});
