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
import { adminApi, type StatusRequest } from "../admin/api";
import { ApiError, client } from "../api/client";
import { adminUser } from "./adminFixtures";
const target = adminUser.id;
const user = { ...adminUser, status: "active", status_version: 0 };
const receipt = {
  user_id: target,
  status: "disabled" as const,
  status_version: 1,
  audit_id: "audit-status",
  duplicate: false,
};
function login(id = "write-operator", token = "local-write-token") {
  authStore.accept({
    status: "authenticated",
    access_token: token,
    user: {
      id,
      nickname: "管理员",
      role: "admin",
      status: "active",
      is_subscribed: true,
    },
  });
}
function mount(path = `/admin/users/${target}`) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <WorkspaceProvider>
        <AdminView />
      </WorkspaceProvider>
    </MemoryRouter>,
  );
}
async function statusDialog() {
  fireEvent.click(await screen.findByRole("button", { name: "停用账号" }));
  return screen.findByRole("dialog", { name: "账号状态操作" });
}
function confirm(dialog: HTMLElement, reason = "用户申请停用") {
  fireEvent.change(within(dialog).getByLabelText("操作原因"), {
    target: { value: reason },
  });
  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: "确认停用" }));
}
beforeEach(() => {
  sessionStorage.clear();
  authStore.clear();
  login();
  vi.spyOn(adminApi, "user").mockResolvedValue({ ...user });
  vi.spyOn(adminApi, "changeStatus").mockResolvedValue(receipt);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  authStore.clear();
  sessionStorage.clear();
});
it("requires reason and explicit confirmation and sends the exact optimistic version", async () => {
  mount();
  const dialog = await statusDialog();
  const submit = within(dialog).getByRole("button", { name: "确认停用" });
  await waitFor(() => expect(submit).toHaveProperty("disabled", true));
  fireEvent.click(submit);
  expect(adminApi.changeStatus).not.toHaveBeenCalled();
  confirm(dialog);
  expect(await within(dialog).findByText(/停用已确认/)).toBeTruthy();
  expect(adminApi.changeStatus).toHaveBeenCalledWith(
    target,
    {
      status: "disabled",
      expected_status: "active",
      expected_version: 0,
      reason: "用户申请停用",
      key: expect.stringMatching(/^web_/),
    },
    expect.any(AbortSignal),
  );
});
it("retains the original immutable request after a timeout and retries only on click", async () => {
  vi.mocked(adminApi.changeStatus).mockRejectedValueOnce(
    new ApiError("REQUEST_TIMEOUT", "提交超时"),
  );
  mount();
  const dialog = await statusDialog();
  confirm(dialog);
  expect(await within(dialog).findByText("结果待确认")).toBeTruthy();
  expect(adminApi.changeStatus).toHaveBeenCalledTimes(1);
  expect(within(dialog).queryByLabelText("操作原因")).toBeNull();
  const first = structuredClone(
    vi.mocked(adminApi.changeStatus).mock.calls[0]![1],
  );
  fireEvent.click(
    within(dialog).getByRole("button", { name: "使用原请求重试" }),
  );
  expect(await within(dialog).findByText(/停用已确认/)).toBeTruthy();
  expect(vi.mocked(adminApi.changeStatus).mock.calls[1]![1]).toEqual(first);
});
it("suppresses repeated submissions and preserves pending state when the dialog closes", async () => {
  let resolve!: (value: typeof receipt) => void;
  vi.mocked(adminApi.changeStatus).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  mount();
  const dialog = await statusDialog();
  confirm(dialog);
  expect(adminApi.changeStatus).toHaveBeenCalledTimes(1);
  fireEvent.click(within(dialog).getByRole("button", { name: "关闭操作窗口" }));
  expect(
    screen.getByRole("complementary", { name: "待确认的管理操作" }),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "查看状态操作" }));
  expect(screen.getByText(/正在提交，请勿重复操作/)).toBeTruthy();
  expect(adminApi.changeStatus).toHaveBeenCalledTimes(1);
  await act(async () => resolve(receipt));
});
it("refreshes a version conflict and requires a new reviewed request", async () => {
  vi.mocked(adminApi.changeStatus).mockRejectedValueOnce(
    new ApiError("USER_STATUS_CONFLICT", "账号状态已变化", "req-conflict", 409),
  );
  mount();
  const dialog = await statusDialog();
  confirm(dialog);
  expect(await within(dialog).findByText(/账号状态或版本已变化/)).toBeTruthy();
  vi.mocked(adminApi.user).mockResolvedValue({ ...user, status_version: 2 });
  fireEvent.click(within(dialog).getByRole("button", { name: "重新核对" }));
  await waitFor(() => expect(adminApi.user).toHaveBeenCalled());
  expect(adminApi.changeStatus).toHaveBeenCalledTimes(1);
});
it("protects administrator targets from status writes", async () => {
  vi.mocked(adminApi.user).mockResolvedValue({ ...user, role: "admin" });
  mount();
  expect(await screen.findByText(/管理员账号受保护/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "停用账号" })).toBeNull();
  expect(adminApi.changeStatus).not.toHaveBeenCalled();
});
it("recovers unknown requests after remount without automatically resubmitting", async () => {
  vi.mocked(adminApi.changeStatus).mockRejectedValueOnce(
    new ApiError("NETWORK_ERROR", "连接中断"),
  );
  const first = mount();
  let dialog = await statusDialog();
  confirm(dialog);
  await within(dialog).findByText("结果待确认");
  const body = vi.mocked(adminApi.changeStatus).mock.calls[0]![1];
  first.unmount();
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "查看状态操作" }));
  dialog = await screen.findByRole("dialog", { name: "账号状态操作" });
  expect(adminApi.changeStatus).toHaveBeenCalledTimes(1);
  expect(within(dialog).getByText(body.key)).toBeTruthy();
  fireEvent.click(
    within(dialog).getByRole("button", { name: "使用原请求重试" }),
  );
  await waitFor(() => expect(adminApi.changeStatus).toHaveBeenCalledTimes(2));
  expect(vi.mocked(adminApi.changeStatus).mock.calls[1]![1]).toEqual(body);
});
it("does not dispatch under a different account when identity changes before Axios sends", async () => {
  vi.mocked(adminApi.changeStatus).mockRestore();
  const adapter = vi.fn();
  const previous = client.defaults.adapter;
  client.defaults.adapter = adapter;
  try {
    const body: StatusRequest = {
      status: "disabled",
      expected_status: "active",
      expected_version: 0,
      reason: "测试",
      key: "web-local",
    };
    const pending = adminApi.changeStatus(target, body);
    login("another-admin", "another-token");
    await expect(pending).rejects.toMatchObject({
      code: "ADMIN_SESSION_CHANGED",
    });
    expect(adapter).not.toHaveBeenCalled();
  } finally {
    client.defaults.adapter = previous;
  }
});

const taskId = "00000000-0000-4000-8000-000000000002";
const preview = {
  product: "monthly" as const,
  offer: {
    name: "月会员",
    asr_seconds: 18000,
    ai_generations: 100,
    price_fen: 1990,
  },
  starts_at: "2026-11-26T03:00:00Z",
  expires_at: "2026-12-26T03:00:00Z",
  member_until_before: "2026-11-26T03:00:00Z",
  mode: "operator_grant" as const,
  payment_verified: false as const,
  note: "预览不锁定时间",
};
async function grantSetup() {
  const { adminBalances } = await import("./adminFixtures");
  vi.spyOn(adminApi, "allowances").mockResolvedValue({
    ...adminBalances,
    account_active: true,
  });
  vi.spyOn(adminApi, "grantPreview").mockImplementation(async (_, product) => ({
    ...preview,
    product,
  }));
  vi.spyOn(adminApi, "grant").mockImplementation(async (_, body) => ({
    key: body.key,
    product: body.product,
    starts_at: preview.starts_at,
    expires_at: preview.expires_at,
    mode: "operator_grant",
    payment_verified: false,
    duplicate: false,
    audit_id: "audit-grant",
    dispatched: null,
    dispatch_pending: true,
  }));
  mount(`/admin/allowances/${target}`);
  fireEvent.click(await screen.findByRole("button", { name: "人工发放权益" }));
  const dialog = await screen.findByRole("dialog", { name: "人工权益发放" });
  await within(dialog).findByText(/月会员 · 转写/);
  return dialog;
}
function confirmGrant(dialog: HTMLElement) {
  fireEvent.change(within(dialog).getByLabelText("操作原因"), {
    target: { value: "人工补发权益" },
  });
  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: "确认人工发放" }));
}
async function cancelSetup() {
  const { task } = await import("./adminFixtures");
  vi.spyOn(adminApi, "task").mockResolvedValue({
    ...task,
    task_id: taskId,
    user_id: target,
    status: "running",
    progress: 30,
  });
  vi.spyOn(adminApi, "cancelTask").mockResolvedValue({
    task_id: taskId,
    status: "running",
    cancel_requested: true,
    notification_pending: true,
    audit_id: "audit-cancel",
    duplicate: false,
  });
  mount(`/admin/tasks/${taskId}`);
  fireEvent.click(await screen.findByRole("button", { name: "请求取消任务" }));
  return screen.findByRole("dialog", { name: "请求取消任务" });
}
function confirmCancel(dialog: HTMLElement) {
  fireEvent.change(within(dialog).getByLabelText("操作原因"), {
    target: { value: "用户请求取消" },
  });
  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: "确认请求取消" }));
}
it("previews fixed products and never sends price, amount or expiry in a grant", async () => {
  const dialog = await grantSetup();
  expect(adminApi.grant).not.toHaveBeenCalled();
  confirmGrant(dialog);
  expect(
    await within(dialog).findByText(/权益已发放，排队任务唤醒待恢复/),
  ).toBeTruthy();
  const body = vi.mocked(adminApi.grant).mock.calls[0]![1];
  expect(Object.keys(body).sort()).toEqual(["key", "product", "reason"]);
  expect(body.product).toBe("monthly");
  expect(screen.queryByText(/已收到付款/)).toBeNull();
  fireEvent.click(within(dialog).getByRole("button", { name: "开始新的发放" }));
  await within(dialog).findByText(/月会员 · 转写/);
  expect(within(dialog).getByRole("checkbox")).toHaveProperty("checked", false);
  expect(within(dialog).getByLabelText("操作原因")).toHaveProperty("value", "");
});
it("locks unknown grant payload and replays the same key without automatic duplicate grants", async () => {
  const dialog = await grantSetup();
  vi.mocked(adminApi.grant).mockRejectedValueOnce(
    new ApiError("NETWORK_ERROR", "连接中断"),
  );
  confirmGrant(dialog);
  await within(dialog).findByText("结果待确认");
  const body = structuredClone(vi.mocked(adminApi.grant).mock.calls[0]![1]);
  expect(within(dialog).queryByLabelText("发放产品")).toBeNull();
  fireEvent.click(
    within(dialog).getByRole("button", { name: "使用原请求重试" }),
  );
  await within(dialog).findByText(/发放已确认/);
  expect(vi.mocked(adminApi.grant).mock.calls[1]![1]).toEqual(body);
});
it("does not revoke the operator's role on target account eligibility errors", async () => {
  const dialog = await grantSetup();
  vi.mocked(adminApi.grantPreview).mockRejectedValue(
    new ApiError("ACCOUNT_NOT_BOUND", "目标账号不可发放", "", 403),
  );
  fireEvent.change(within(dialog).getByLabelText("发放产品"), {
    target: { value: "ai_100" },
  });
  expect(await within(dialog).findByText("目标账号不可发放")).toBeTruthy();
  expect(
    screen.queryByRole("heading", { name: "管理权限需要重新确认" }),
  ).toBeNull();
  expect(adminApi.grant).not.toHaveBeenCalled();
});
it("reports accepted cancellation without claiming a cancelled terminal state", async () => {
  const dialog = await cancelSetup();
  confirmCancel(dialog);
  expect(
    await within(dialog).findByText(/已请求取消，等待 Worker/),
  ).toBeTruthy();
  expect(within(dialog).queryByText("服务器已确认任务取消。")).toBeNull();
  expect(within(dialog).getByText(/取消意图已保存，通知待恢复/)).toBeTruthy();
});
it("preserves completed results when cancellation loses a completion race", async () => {
  const dialog = await cancelSetup();
  vi.mocked(adminApi.cancelTask).mockResolvedValue({
    task_id: taskId,
    status: "completed",
    cancel_requested: false,
    notification_pending: false,
    audit_id: "audit-race",
    duplicate: false,
  });
  confirmCancel(dialog);
  expect(await within(dialog).findByText(/任务已完成，未取消/)).toBeTruthy();
});
it("marks cancellation terminal only after the server explicitly confirms it", async () => {
  const dialog = await cancelSetup();
  vi.mocked(adminApi.cancelTask).mockResolvedValue({
    task_id: taskId,
    status: "cancelled",
    cancel_requested: true,
    notification_pending: false,
    audit_id: "audit-done",
    duplicate: false,
  });
  confirmCancel(dialog);
  expect(
    await within(dialog).findByText("服务器已确认任务取消。"),
  ).toBeTruthy();
});
it("persists pending request before sending and refuses submission if storage fails", async () => {
  mount();
  const dialog = await statusDialog();
  const write = vi
    .spyOn(Object.getPrototypeOf(sessionStorage), "setItem")
    .mockImplementation(() => {
      throw new Error("storage unavailable");
    });
  confirm(dialog);
  expect(await within(dialog).findByText(/无法保存操作编号/)).toBeTruthy();
  expect(adminApi.changeStatus).not.toHaveBeenCalled();
  write.mockRestore();
});
it("ignores an old actor's write receipt after account switch", async () => {
  let resolve!: (value: typeof receipt) => void;
  vi.mocked(adminApi.changeStatus).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  mount();
  const dialog = await statusDialog();
  confirm(dialog);
  const signal = vi.mocked(adminApi.changeStatus).mock.calls[0]![2]!;
  act(() => login("new-operator", "new-token"));
  expect(signal.aborted).toBe(true);
  await act(async () => resolve(receipt));
  expect(screen.queryByText(/停用已确认/)).toBeNull();
  expect(
    screen.queryByRole("complementary", { name: "待确认的管理操作" }),
  ).toBeNull();
});
it("keeps malformed 200 responses uncertain instead of acknowledging success", async () => {
  vi.mocked(adminApi.changeStatus).mockResolvedValue({
    status: "disabled",
  } as never);
  mount();
  const dialog = await statusDialog();
  confirm(dialog);
  expect(await within(dialog).findByText("结果待确认")).toBeTruthy();
  expect(within(dialog).queryByText(/停用已确认/)).toBeNull();
});
it("stops the admin session on a write permission rejection", async () => {
  vi.mocked(adminApi.changeStatus).mockRejectedValue(
    new ApiError("FORBIDDEN", "管理员权限已失效", "", 403),
  );
  mount();
  const dialog = await statusDialog();
  confirm(dialog);
  expect(
    await screen.findByRole("heading", { name: "管理权限需要重新确认" }),
  ).toBeTruthy();
});

it("requires fresh confirmation and uses a fresh version/key after a definite conflict", async () => {
  vi.mocked(adminApi.changeStatus).mockRejectedValueOnce(
    new ApiError("USER_STATUS_CONFLICT", "版本已变化", "", 409),
  );
  mount();
  const dialog = await statusDialog();
  confirm(dialog);
  await within(dialog).findByText("请求被拒绝");
  const previous = vi.mocked(adminApi.changeStatus).mock.calls[0]![1];
  vi.mocked(adminApi.user).mockResolvedValue({ ...user, status_version: 2 });
  vi.mocked(adminApi.changeStatus).mockResolvedValue({
    ...receipt,
    status_version: 3,
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "重新核对" }));
  expect(await within(dialog).findByRole("checkbox")).toHaveProperty(
    "checked",
    false,
  );
  await waitFor(() =>
    expect(
      within(dialog).getByRole("button", { name: "确认停用" }),
    ).toHaveProperty("disabled", true),
  );
  confirm(dialog, "再次核对后停用");
  await within(dialog).findByText(/停用已确认/);
  const next = vi.mocked(adminApi.changeStatus).mock.calls[1]![1];
  expect(next.expected_version).toBe(2);
  expect(next.key).not.toBe(previous.key);
});
it("disables mutation controls for cookie-only read sessions", async () => {
  authStore.clear();
  vi.spyOn(client, "get").mockResolvedValueOnce({
    data: {
      id: "write-operator",
      nickname: "管理员",
      role: "admin",
      status: "active",
      is_subscribed: true,
    },
  });
  await authStore.restore();
  mount();
  expect(await screen.findByText("重新登录以启用管理操作")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "停用账号" })).toBeNull();
  expect(adminApi.changeStatus).not.toHaveBeenCalled();
});
it("recovers a request interrupted while pending as uncertain after remount", async () => {
  vi.mocked(adminApi.changeStatus).mockImplementation(
    () => new Promise(() => {}),
  );
  const first = mount();
  const dialog = await statusDialog();
  confirm(dialog);
  await waitFor(() => expect(adminApi.changeStatus).toHaveBeenCalled());
  const body = vi.mocked(adminApi.changeStatus).mock.calls[0]![1];
  first.unmount();
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "查看状态操作" }));
  const recovered = await screen.findByRole("dialog", { name: "账号状态操作" });
  expect(within(recovered).getByText("结果待确认")).toBeTruthy();
  expect(within(recovered).getByText(body.key)).toBeTruthy();
  expect(adminApi.changeStatus).toHaveBeenCalledTimes(1);
});
it.each(["IDEMPOTENCY_CONFLICT", "LEGACY_GRANT_EXISTS"])(
  "does not allow a fresh key when %s needs reconciliation",
  async (code) => {
    const dialog = await grantSetup();
    vi.mocked(adminApi.grant).mockRejectedValue(
      new ApiError(code, "请核查已有发放", "", 409),
    );
    confirmGrant(dialog);
    await within(dialog).findByText("结果待确认");
    expect(
      within(dialog).queryByRole("button", { name: "重新核对" }),
    ).toBeNull();
    expect(
      within(dialog).queryByRole("button", { name: "开始新的发放" }),
    ).toBeNull();
  },
);
it("sends admin write contracts with a guarded identity and bounded timeout", async () => {
  vi.mocked(adminApi.changeStatus).mockRestore();
  const post = vi.spyOn(client, "post").mockResolvedValue({ data: receipt });
  const controller = new AbortController();
  const body: StatusRequest = {
    status: "disabled",
    expected_status: "active",
    expected_version: 0,
    reason: "合同测试",
    key: "local-key",
  };
  await adminApi.changeStatus(target, body, controller.signal);
  expect(post).toHaveBeenCalledWith(`/admin/users/${target}/status`, body, {
    signal: controller.signal,
    timeout: 15000,
    adminIdentity: { userId: "write-operator", token: "local-write-token" },
  });
});

it("keeps a confirmed receipt visible even if the following detail refresh fails", async () => {
  mount();
  const dialog = await statusDialog();
  vi.mocked(adminApi.user).mockRejectedValue(
    new ApiError("UNAVAILABLE", "刷新暂不可用", "", 503),
  );
  confirm(dialog);
  await screen.findByText("刷新暂不可用");
  expect(
    screen.getByRole("complementary", { name: "已确认的管理操作" }),
  ).toBeTruthy();
  expect(adminApi.changeStatus).toHaveBeenCalledTimes(1);
});
