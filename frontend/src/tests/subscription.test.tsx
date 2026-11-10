// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
  act,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import AccountView from "../views/AccountView";
import { WorkspaceProvider } from "../hooks/useWorkspace";
import SubscriptionView from "../views/SubscriptionView";
import AllowanceConfirm from "../components/AllowanceConfirm";
import { api } from "../api";
import { ApiError } from "../api/client";
import { authStore } from "../stores/auth";
import { billingStore } from "../stores/billing";
import { catalog, allowances, quote } from "./billingFixtures";
const user = {
  id: "billing-a",
  nickname: "体验用户",
  is_subscribed: true,
  role: "user",
  status: "active",
} as const;
function login(id: string = user.id) {
  authStore.accept({
    status: "authenticated",
    access_token: "test-token",
    user: { ...user, id },
  });
}
function mount(path = "/subscription") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <WorkspaceProvider>
        <Routes>
          <Route path="/subscription" element={<SubscriptionView />} />
          <Route path="/account" element={<AccountView />} />
        </Routes>
      </WorkspaceProvider>
    </MemoryRouter>,
  );
}
beforeEach(() => {
  authStore.clear();
  authStore.close();
  billingStore.setUser(null);
  vi.spyOn(api, "catalog").mockResolvedValue(catalog);
  vi.spyOn(api, "allowances").mockResolvedValue(allowances);
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
  billingStore.setUser(null);
  authStore.clear();
  authStore.close();
  vi.restoreAllMocks();
  vi.useRealTimers();
});
it("shows the three approved offers without taking payments before entitlement delivery is ready", async () => {
  mount();
  expect(screen.getByRole("heading", { name: "免费体验" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Plus" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Pro" })).toBeTruthy();
  expect(
    within(screen.getByRole("article", { name: "Plus" })).getByText("¥9.9"),
  ).toBeTruthy();
  expect(screen.getByText("¥16.9")).toBeTruthy();
  expect(screen.getByText("每日 2 次视频解析")).toBeTruthy();
  expect(screen.getByText("每日 10 GB 下载流量")).toBeTruthy();
  expect(screen.getByText("每日下载流量不限量")).toBeTruthy();
  for (const minutes of [20, 60, 120])
    expect(screen.getByText(`单视频最长 ${minutes} 分钟`)).toBeTruthy();
  screen.getByRole("button", { name: "选择 Pro" }).focus();
  fireEvent.click(screen.getByRole("button", { name: "选择 Pro" }));
  const dialog = screen.getByRole("dialog", { name: "Pro · 月付" });
  expect(within(dialog).getByText(/暂未开放在线购买/)).toBeTruthy();
  expect(within(dialog).queryByRole("button", { name: /支付/ })).toBeNull();
  fireEvent.click(within(dialog).getByRole("button", { name: "我知道了" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(
    screen.getByRole("button", { name: "选择 Pro" }),
  );
});
it("publishes the proposed prices independently of the legacy live entitlement catalog", () => {
  vi.mocked(api.catalog).mockRejectedValue(
    new ApiError("NETWORK_ERROR", "加载失败"),
  );
  mount();
  expect(screen.getByText("¥16.9")).toBeTruthy();
  expect(screen.getByText(/新方案待开放/)).toBeTruthy();
  expect(api.catalog).not.toHaveBeenCalled();
  expect(api.allowances).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "购买AI 额度包" }));
  const dialog = screen.getByRole("dialog", { name: "AI 额度包" });
  expect(within(dialog).getByText("50 次 · 90 天有效")).toBeTruthy();
  expect(within(dialog).getByText("¥5.9")).toBeTruthy();
});
it("switches all cycle totals while retaining monthly quotas and confirms the selected annual offer", () => {
  mount();
  const periods = within(screen.getByRole("group", { name: "付费周期" }));
  for (const [label, plus, pro] of [
    ["季付", "¥26.9", "¥45.9"],
    ["年付", "¥99", "¥169"],
    ["月付", "¥9.9", "¥16.9"],
  ]) {
    fireEvent.click(periods.getByRole("button", { name: new RegExp(label) }));
    expect(
      within(screen.getByRole("article", { name: "Plus" })).getByText(plus),
    ).toBeTruthy();
    expect(screen.getByText(pro)).toBeTruthy();
    expect(screen.getByText("150 分钟 / 月")).toBeTruthy();
    expect(screen.getByText("100 次 / 月")).toBeTruthy();
    expect(
      periods
        .getAllByRole("button")
        .filter((button) => button.getAttribute("aria-pressed") === "true"),
    ).toHaveLength(1);
  }
  fireEvent.click(periods.getByRole("button", { name: /年付/ }));
  expect(screen.getByText("一次支付 ¥169，折合 ¥14.08 / 月")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "选择 Plus" }));
  const dialog = screen.getByRole("dialog", { name: "Plus · 年付" });
  expect(within(dialog).getByText("¥99")).toBeTruthy();
  expect(within(dialog).getByText(/服务期 12 个月/)).toBeTruthy();
  expect(within(dialog).queryByRole("link", { name: /前往支付/ })).toBeNull();
});
it("shows real balances, held usage and expiry without exposing the previous account", async () => {
  login();
  await billingStore.refresh();
  mount("/account");
  expect(screen.getByText("29 分 59 秒")).toBeTruthy();
  expect(screen.getByText(/执行中占用 1 秒/)).toBeTruthy();
  fireEvent.click(screen.getByText("额度明细"));
  expect(screen.getByText(/2099.*到期/)).toBeTruthy();
  act(() => authStore.clear());
  expect(screen.queryByText("29 分 59 秒")).toBeNull();
  expect(screen.getByRole("button", { name: "登录查看个人中心" })).toBeTruthy();
});
it("ignores an old account balance response after switching users", async () => {
  login();
  let finish!: (value: typeof allowances) => void;
  vi.mocked(api.allowances).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const pending = billingStore.refresh();
  login("billing-b");
  billingStore.setUser("billing-b");
  finish(allowances);
  await pending;
  expect(billingStore.getState().userId).toBe("billing-b");
  expect(billingStore.getState().data).toBeNull();
});
it("shows insufficient allowance as queued confirmation, with a cancel that does not submit", async () => {
  login();
  await billingStore.refresh();
  const onConfirm = vi.fn(),
    onClose = vi.fn();
  render(
    <MemoryRouter>
      <AllowanceConfirm
        quote={{
          ...quote,
          task_type: "ai_summary",
          maximum: { ...quote.maximum, asr_seconds: 2000, ai_generations: 1 },
        }}
        label="生成总结"
        pending={false}
        error={null}
        onConfirm={onConfirm}
        onClose={onClose}
        onRefresh={vi.fn()}
      />
    </MemoryRouter>,
  );
  expect(screen.getByRole("button", { name: "确认并排队" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "暂不开始" }));
  expect(onClose).toHaveBeenCalledOnce();
  expect(onConfirm).not.toHaveBeenCalled();
});
it("requires a new quote once expired and blocks duplicate submission while pending", () => {
  const onRefresh = vi.fn(),
    onConfirm = vi.fn();
  const props = {
    quote: { ...quote, expires_at: "2000-01-01T00:00:00Z" },
    label: "下载",
    pending: false,
    error: null,
    onConfirm,
    onClose: vi.fn(),
    onRefresh,
  };
  const view = render(
    <MemoryRouter>
      <AllowanceConfirm {...props} />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "重新获取报价" }));
  expect(onRefresh).toHaveBeenCalledOnce();
  expect(onConfirm).not.toHaveBeenCalled();
  view.rerender(
    <MemoryRouter>
      <AllowanceConfirm {...props} pending />
    </MemoryRouter>,
  );
  expect(
    (screen.getByRole("button", { name: "正在提交…" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
});

it("opens personal allowances from the logged-in trial action and returns to plans", async () => {
  login();
  mount();
  fireEvent.click(
    await screen.findByRole("button", { name: "查看我的体验额度" }),
  );
  expect(await screen.findByRole("heading", { name: "个人中心" })).toBeTruthy();
  expect(await screen.findByText("29 分 59 秒")).toBeTruthy();
  fireEvent.click(screen.getByRole("link", { name: "查看订阅方案" }));
  expect(await screen.findByRole("heading", { name: "免费体验" })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "我的额度" })).toBeNull();
});
it("keeps account balances visible when the download catalog fails and retries the cap", async () => {
  login();
  vi.mocked(api.catalog).mockRejectedValueOnce(
    new ApiError("NETWORK_ERROR", "加载失败"),
  );
  mount("/account");
  expect(await screen.findByText("29 分 59 秒")).toBeTruthy();
  expect(await screen.findByText("1 / —")).toBeTruthy();
  fireEvent.click(await screen.findByRole("button", { name: "重新读取上限" }));
  expect(await screen.findByText("1 / 2")).toBeTruthy();
});
it("retries a failed allowance request without showing invented balances", async () => {
  login();
  vi.mocked(api.allowances).mockRejectedValueOnce(
    new ApiError("NETWORK_ERROR", "额度读取失败"),
  );
  mount("/account");
  expect(await screen.findByText("额度读取失败")).toBeTruthy();
  expect(screen.queryByText("0 分钟")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "刷新额度" }));
  expect(await screen.findByText("29 分 59 秒")).toBeTruthy();
});
it("keeps paid membership separate from following status", async () => {
  login();
  vi.mocked(api.allowances).mockResolvedValue({
    ...allowances,
    member: true,
    member_until: "2099-01-01T00:00:00Z",
    subscribed: false,
  });
  mount("/account");
  expect(await screen.findByText("月度会员")).toBeTruthy();
  expect(screen.getByText("公众号未关注")).toBeTruthy();
  expect(screen.getByText("不按次数计费")).toBeTruthy();
  fireEvent.click(screen.getByText("额度明细"));
  expect(screen.getByText("暂不可用 · 需关注公众号")).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "我的知识库" }).getAttribute("href"),
  ).toBe("/library");
});

it("identifies both addon products in personal allowance details", async () => {
  login();
  vi.mocked(api.allowances).mockResolvedValue({
    ...allowances,
    allowances: [
      { ...allowances.allowances[0]!, source: "asr_120" },
      {
        ...allowances.allowances[0]!,
        source: "ai_100",
        kind: "ai_generations",
        granted: 100,
      },
    ],
  });
  mount("/account");
  expect(await screen.findByText("120 分钟转写包 · 转写额度")).toBeTruthy();
  expect(screen.getByText("100 次 AI 包 · AI额度")).toBeTruthy();
  expect(screen.queryByText(/其他.*额度/)).toBeNull();
});

it.each([
  ["Plus", "月付", "¥9.9"],
  ["Pro", "月付", "¥16.9"],
  ["Plus", "季付", "¥26.9"],
  ["Pro", "季付", "¥45.9"],
  ["Plus", "年付", "¥99"],
  ["Pro", "年付", "¥169"],
])("keeps payments closed for %s %s and addons", (plan, period, price) => {
  mount();
  fireEvent.click(
    within(screen.getByRole("group", { name: "付费周期" })).getByRole(
      "button",
      { name: new RegExp(period) },
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: `选择 ${plan}` }));
  const dialog = screen.getByRole("dialog", { name: `${plan} · ${period}` });
  const link = within(dialog).queryByRole("link", { name: /前往支付/ });
  expect(within(dialog).getByText(price)).toBeTruthy();
  expect(link).toBeNull();
  expect(within(dialog).getByText(/暂未开放在线购买/)).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("button", { name: "我知道了" }));
  fireEvent.click(screen.getByRole("button", { name: "购买转写额度包" }));
  expect(
    within(screen.getByRole("dialog", { name: "转写额度包" })).queryByRole(
      "link",
      { name: /前往支付/ },
    ),
  ).toBeNull();
});
