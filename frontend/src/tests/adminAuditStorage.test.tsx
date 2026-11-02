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
import { adminApi, type AuditEvent, type CleanupReceipt } from "../admin/api";
import { readAuditFilters } from "../admin/AuditPage";
import { api } from "../api";
import { ApiError, client } from "../api/client";
import { storage } from "./adminFixtures";
const actor = "00000000-0000-4000-8000-000000000010";
const target = "00000000-0000-4000-8000-000000000020";
const event: AuditEvent = {
  id: "00000000-0000-4000-8000-000000000030",
  actor_kind: "user",
  actor_label: actor,
  actor_user_id: actor,
  action: "task.cancel",
  target_type: "task",
  target_id: target,
  reason: "用户申请取消",
  request_id: "req_audit_fixture",
  idempotency_key: "web_fixture",
  outcome: "accepted",
  changes: { cancel_requested: true },
  request_data: {},
  result: { status: "running" },
  created_at: "2026-10-26T03:00:00Z",
  current_task_status: "completed",
};
const receipt: CleanupReceipt = {
  operation_id: "cleanup-fixture",
  state: "completed",
  audit_id: "audit-cleanup",
  files: 2,
  orphan_files: 1,
  temporary_directories: 1,
  freed_bytes: 2048,
  used_bytes: 0,
};
function mount(path = "/admin/system") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <WorkspaceProvider>
        <AdminView />
      </WorkspaceProvider>
    </MemoryRouter>,
  );
}
async function openCleanup() {
  fireEvent.click(await screen.findByRole("button", { name: "清理过期文件" }));
  return screen.findByRole("dialog", { name: "清理过期文件" });
}
function submit(dialog: HTMLElement) {
  fireEvent.change(within(dialog).getByLabelText("操作原因"), {
    target: { value: "清理隔离测试过期文件" },
  });
  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(
    within(dialog).getByRole("button", { name: "确认清理过期文件" }),
  );
}
beforeEach(() => {
  sessionStorage.clear();
  authStore.clear();
  authStore.accept({
    status: "authenticated",
    access_token: "local-audit-fixture-token",
    user: {
      id: actor,
      role: "admin",
      status: "active",
      nickname: "测试管理员",
      is_subscribed: true,
    },
  });
  vi.spyOn(adminApi, "storage").mockResolvedValue({ ...storage });
  vi.spyOn(api, "ready").mockResolvedValue({
    status: "ready",
    database: "ready",
    redis: "ready",
  });
  vi.spyOn(adminApi, "cleanup").mockResolvedValue({ ...receipt });
  vi.spyOn(adminApi, "audit").mockResolvedValue({
    items: [event],
    limit: 20,
    offset: 0,
    total: 21,
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  authStore.clear();
  sessionStorage.clear();
});
it("preserves historical accepted outcome beside current completed state and exposes read-only details", async () => {
  mount("/admin/audit");
  expect(await screen.findByText("已接受")).toBeTruthy();
  expect(screen.getByText("已完成")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /查看审计/ }));
  const dialog = await screen.findByRole("dialog", { name: "审计详情" });
  expect(within(dialog).getByText(event.request_id)).toBeTruthy();
  expect(within(dialog).getByText(event.idempotency_key)).toBeTruthy();
  expect(within(dialog).getByText(/不代表进程退出/)).toBeTruthy();
  expect(within(dialog).getByRole("link", { name: target })).toHaveProperty(
    "pathname",
    `/admin/tasks/${target}`,
  );
  expect(
    within(dialog).queryByRole("button", { name: /修改|删除/ }),
  ).toBeNull();
  fireEvent.keyDown(dialog, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});
it("keeps audit filters while paginating and resets pagination on search", async () => {
  mount(`/admin/audit?target_type=task&target_id=${target}`);
  await screen.findByText("已接受");
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(adminApi.audit).toHaveBeenLastCalledWith(
      expect.objectContaining({
        target_type: "task",
        target_id: target,
        offset: 20,
      }),
      expect.any(AbortSignal),
    ),
  );
  fireEvent.change(screen.getByLabelText("操作动作"), {
    target: { value: "task.cancel" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() =>
    expect(adminApi.audit).toHaveBeenLastCalledWith(
      expect.objectContaining({ action: "task.cancel", offset: 0 }),
      expect.any(AbortSignal),
    ),
  );
});
it.each([
  "actor_id=bad",
  "action=toString",
  "outcome=__proto__",
  "target_type=constructor",
  "since=not-a-date",
  "since=2026-10-27T00:00&until=2026-10-26T00:00",
])("blocks malformed audit queries: %s", async (params) => {
  mount(`/admin/audit?${params}`);
  expect(await screen.findByText("请修正筛选后查询。")).toBeTruthy();
  expect(adminApi.audit).not.toHaveBeenCalled();
});
it("converts Beijing dates to an explicit audit interval", () => {
  const result = readAuditFilters(
    new URLSearchParams("since=2026-10-26T08:00&until=2026-10-27T08:00"),
  );
  expect(result.error).toBe("");
  expect(result.filters).toMatchObject({
    since: "2026-10-26T00:00:00.000Z",
    until: "2026-10-27T00:00:00.000Z",
  });
});
it("shows incomplete scans as unknown without a misleading percentage and refreshes on demand", async () => {
  vi.mocked(adminApi.storage).mockResolvedValue({
    ...storage,
    status: "partial",
    used_bytes: null,
    scan_complete: false,
    observed_bytes: 1024,
    disk_free_bytes: 0,
  });
  mount();
  expect(await screen.findByText(/总占用未知，不能计算占用比例/)).toBeTruthy();
  expect(screen.queryByRole("progressbar")).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain("低于安全阈值");
  expect(adminApi.storage).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "刷新存储" }));
  await waitFor(() => expect(adminApi.storage).toHaveBeenCalledTimes(2));
});
it("requires reason and confirmation and accepts cleanup receipts without a duplicate flag", async () => {
  mount();
  const dialog = await openCleanup();
  const button = within(dialog).getByRole("button", {
    name: "确认清理过期文件",
  });
  expect(button).toHaveProperty("disabled", true);
  fireEvent.click(button);
  expect(adminApi.cleanup).not.toHaveBeenCalled();
  submit(dialog);
  expect(
    await within(dialog).findByText("清理已完成，已释放 2.0 KiB。"),
  ).toBeTruthy();
  expect(within(dialog).getByText(/处理文件 3 个/)).toBeTruthy();
  expect(within(dialog).getByText(/审计编号：audit-cleanup/)).toBeTruthy();
  const body = vi.mocked(adminApi.cleanup).mock.calls[0]![0];
  expect(Object.keys(body).sort()).toEqual(["key", "reason"]);
  expect(body.key).toMatch(/^web_/);
  expect(
    within(dialog).getByRole("link", { name: "查看相关审计 →" }),
  ).toHaveProperty("search", "?target_type=storage&target_id=local&action=");
  await waitFor(() => expect(adminApi.storage).toHaveBeenCalledTimes(2));
});
it.each(["accepted", "outcome_unrecorded"] as const)(
  "locks %s cleanup to its original key after remount",
  async (state) => {
    vi.mocked(adminApi.cleanup).mockResolvedValueOnce({
      operation_id: "cleanup-fixture",
      audit_id: "audit-intent",
      state,
      requires_review: true,
    });
    const first = mount();
    const dialog = await openCleanup();
    submit(dialog);
    await within(dialog).findByText("结果待确认");
    const original = structuredClone(
      vi.mocked(adminApi.cleanup).mock.calls[0]![0],
    );
    expect(within(dialog).queryByLabelText("操作原因")).toBeNull();
    expect(
      within(dialog).queryByRole("button", { name: "重新核对" }),
    ).toBeNull();
    first.unmount();
    mount();
    fireEvent.click(
      await screen.findByRole("button", { name: "查看清理操作" }),
    );
    const recovered = await screen.findByRole("dialog", {
      name: "清理过期文件",
    });
    expect(adminApi.cleanup).toHaveBeenCalledTimes(1);
    fireEvent.click(
      within(recovered).getByRole("button", { name: "核对原清理结果" }),
    );
    await within(recovered).findByText(/清理已完成/);
    expect(vi.mocked(adminApi.cleanup).mock.calls[1]![0]).toEqual(original);
  },
);
it("does not retry cleanup automatically after a server timeout", async () => {
  vi.mocked(adminApi.cleanup).mockRejectedValueOnce(
    new ApiError("STORAGE_CLEANUP_TIMEOUT", "清理超时", "req-timeout", 503),
  );
  mount();
  const dialog = await openCleanup();
  submit(dialog);
  await within(dialog).findByText("结果待确认");
  expect(adminApi.cleanup).toHaveBeenCalledTimes(1);
  expect(within(dialog).queryByRole("button", { name: "重新核对" })).toBeNull();
});
it("describes failed-or-partial cleanup honestly and requires confirmation again", async () => {
  vi.mocked(adminApi.cleanup).mockResolvedValueOnce({
    operation_id: "cleanup-fixture",
    audit_id: "audit-partial",
    state: "failed_or_partial",
    duplicate: true,
  });
  mount();
  const dialog = await openCleanup();
  submit(dialog);
  expect(await within(dialog).findByText("清理失败或部分完成")).toBeTruthy();
  expect(within(dialog).queryByText(/清理已完成/)).toBeNull();
  expect(within(dialog).getByText(/审计编号：audit-partial/)).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("button", { name: "重新核对" }));
  expect(await within(dialog).findByRole("checkbox")).toHaveProperty(
    "checked",
    false,
  );
  expect(
    within(dialog).getByRole("button", { name: "确认清理过期文件" }),
  ).toHaveProperty("disabled", true);
});
it("keeps malformed cleanup success receipts uncertain", async () => {
  vi.mocked(adminApi.cleanup).mockResolvedValueOnce({
    ...receipt,
    used_bytes: undefined,
  });
  mount();
  const dialog = await openCleanup();
  submit(dialog);
  expect(await within(dialog).findByText("结果待确认")).toBeTruthy();
});
it("bounds cleanup request time beyond the backend execution budget", async () => {
  vi.mocked(adminApi.cleanup).mockRestore();
  const post = vi.spyOn(client, "post").mockResolvedValue({ data: receipt });
  const controller = new AbortController();
  const body = { reason: "测试清理", key: "fixture" };
  await adminApi.cleanup(body, controller.signal);
  expect(post).toHaveBeenCalledWith("/admin/storage/cleanup", body, {
    signal: controller.signal,
    timeout: 110000,
    adminIdentity: { userId: actor, token: "local-audit-fixture-token" },
  });
});
it.each(["/admin/audit", "/admin/system"])(
  "removes protected %s after logout",
  async (path) => {
    mount(path);
    if (path.endsWith("audit")) await screen.findByText("已接受");
    else await screen.findByRole("button", { name: "清理过期文件" });
    act(() => authStore.clear());
    expect(await screen.findByText("此页面仅供管理员访问。")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "清理过期文件" })).toBeNull();
  },
);
