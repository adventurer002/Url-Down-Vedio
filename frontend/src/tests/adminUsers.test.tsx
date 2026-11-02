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
import { client } from "../api/client";
import { adminUser, adminBalances } from "./adminFixtures";
function mount(path = "/admin/users") {
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
    access_token: "local-user-test",
    user: {
      id: "admin-test",
      nickname: "管理员",
      role: "admin",
      status: "active",
      is_subscribed: true,
    },
  });
  vi.spyOn(adminApi, "users").mockResolvedValue({
    items: [adminUser],
    total: 1,
    limit: 20,
    offset: 0,
  });
  vi.spyOn(adminApi, "user").mockResolvedValue(adminUser);
  vi.spyOn(adminApi, "allowances").mockResolvedValue(
    structuredClone(adminBalances),
  );
  vi.spyOn(adminApi, "ledger").mockResolvedValue({
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  authStore.clear();
});
it("filters users without conflating subscription, membership or disabled status", async () => {
  mount("/admin/users?offset=40");
  await screen.findByText("测试用户");
  fireEvent.change(screen.getByLabelText("昵称或用户编号"), {
    target: { value: "测试%_&" },
  });
  fireEvent.change(screen.getByLabelText("账号状态"), {
    target: { value: "disabled" },
  });
  fireEvent.change(screen.getByLabelText("公众号关注"), {
    target: { value: "false" },
  });
  fireEvent.change(screen.getByLabelText("会员状态"), {
    target: { value: "true" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() =>
    expect(adminApi.users).toHaveBeenLastCalledWith(
      {
        limit: 20,
        offset: 0,
        q: "测试%_&",
        status: "disabled",
        subscribed: "false",
        member: "true",
      },
      expect.any(AbortSignal),
    ),
  );
});
it("navigates from a filtered user list to details and retains filters on return", async () => {
  mount("/admin/users?status=disabled&member=true");
  fireEvent.click(
    await screen.findByRole("link", { name: "查看用户 测试用户" }),
  );
  expect(await screen.findByText("2", { selector: "dd" })).toBeTruthy();
  expect(screen.getByText(adminUser.id, { selector: "dd" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "停用账号" })).toBeNull();
  fireEvent.click(screen.getByRole("link", { name: "返回用户列表" }));
  await screen.findByText("测试用户");
  expect(screen.getByLabelText("账号状态")).toHaveProperty("value", "disabled");
  expect(screen.getByLabelText("会员状态")).toHaveProperty("value", "true");
});
it("shows usable, retained and held quotas separately for a disabled account", async () => {
  mount(`/admin/allowances/${adminUser.id}`);
  await screen.findByText("20 分 0 秒");
  const asr = screen
    .getByRole("heading", { name: "转写额度" })
    .closest("section")!;
  expect(within(asr).getByText("0 分 0 秒")).toBeTruthy();
  expect(within(asr).getByText("1 分 0 秒")).toBeTruthy();
  expect(screen.getByText("未生效", { selector: "span" })).toBeTruthy();
  expect(screen.getByText("已过期", { selector: "span" })).toBeTruthy();
  expect(adminApi.ledger).not.toHaveBeenCalled();
});
it("paginates lots and ledger independently and loads ledger only on demand", async () => {
  vi.mocked(adminApi.allowances).mockResolvedValue({
    ...adminBalances,
    lots: { ...adminBalances.lots, total: 41 },
  });
  vi.mocked(adminApi.ledger).mockResolvedValue({
    items: [
      {
        id: "grant-fixture",
        event: "grant",
        detail: { ai_generations: 100 },
        created_at: adminBalances.as_of,
      },
    ],
    total: 41,
    limit: 20,
    offset: 0,
  });
  mount(`/admin/allowances/${adminUser.id}?member=true`);
  await screen.findByText("20 分 0 秒");
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "批次分页" })).getByRole(
      "button",
      { name: "下一页" },
    ),
  );
  await waitFor(() =>
    expect(adminApi.allowances).toHaveBeenLastCalledWith(
      adminUser.id,
      20,
      expect.any(AbortSignal),
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "查看额度流水" }));
  expect(await screen.findByText("发放权益")).toBeTruthy();
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "流水分页" })).getByRole(
      "button",
      { name: "下一页" },
    ),
  );
  await waitFor(() =>
    expect(adminApi.ledger).toHaveBeenLastCalledWith(
      adminUser.id,
      20,
      expect.any(AbortSignal),
    ),
  );
  expect(adminApi.allowances).toHaveBeenCalledTimes(2);
  expect(
    within(screen.getByRole("navigation", { name: "批次分页" })).getByText(
      "第 2 页",
    ),
  ).toBeTruthy();
});
it("reads uninitialized accounts without any write or automatic trial claim", async () => {
  const post = vi.spyOn(client, "post");
  vi.mocked(adminApi.allowances).mockResolvedValue({
    ...adminBalances,
    account_exists: false,
    lots: { items: [], total: 0, limit: 20, offset: 0 },
  });
  mount(`/admin/allowances/${adminUser.id}`);
  expect(await screen.findByText(/尚未建立额度账户/)).toBeTruthy();
  expect(post).not.toHaveBeenCalled();
});
it("rejects malformed user detail URLs before fetching private metadata", () => {
  mount("/admin/users/invalid");
  expect(screen.getByRole("alert").textContent).toContain("完整 UUID");
  expect(adminApi.user).not.toHaveBeenCalled();
  expect(adminApi.allowances).not.toHaveBeenCalled();
});
it("serializes user search and explicit false filters using only GET", async () => {
  vi.mocked(adminApi.users).mockRestore();
  const get = vi
    .spyOn(client, "get")
    .mockResolvedValue({ data: { items: [] } });
  await adminApi.users({
    limit: 20,
    q: "测试%_&",
    member: "false",
    subscribed: "false",
  });
  expect(get).toHaveBeenCalledWith(
    "/admin/users?limit=20&q=%E6%B5%8B%E8%AF%95%25_%26&member=false&subscribed=false",
    { signal: undefined, timeout: 5000 },
  );
});

it("does not reuse a previous user's late allowance response", async () => {
  const second = {
    ...adminUser,
    id: "00000000-0000-4000-8000-000000000009",
    nickname: "另一用户",
  };
  vi.mocked(adminApi.users).mockResolvedValue({
    items: [adminUser, second],
    total: 2,
    limit: 20,
    offset: 0,
  });
  vi.mocked(adminApi.user).mockImplementation(async (id) =>
    id === second.id ? second : adminUser,
  );
  let resolve!: (value: typeof adminBalances) => void;
  vi.mocked(adminApi.allowances).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  mount("/admin/allowances");
  expect(adminApi.allowances).not.toHaveBeenCalled();
  fireEvent.click(
    await screen.findByRole("link", { name: "查看额度 测试用户" }),
  );
  await waitFor(() => expect(adminApi.allowances).toHaveBeenCalled());
  const signal = vi.mocked(adminApi.allowances).mock.calls[0]![2]!;
  fireEvent.click(screen.getByRole("link", { name: "返回权益用户列表" }));
  expect(signal.aborted).toBe(true);
  fireEvent.click(
    await screen.findByRole("link", { name: "查看额度 另一用户" }),
  );
  await screen.findByText("20 分 0 秒");
  await act(async () =>
    resolve({
      ...adminBalances,
      balances: {
        ...adminBalances.balances,
        asr_seconds: { available: 600000, usable: 600000, held: 0 },
      },
    }),
  );
  expect(screen.queryByText("10000 分 0 秒")).toBeNull();
});
