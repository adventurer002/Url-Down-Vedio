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
import { MemoryRouter } from "react-router-dom";
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
function mount() {
  return render(
    <MemoryRouter>
      <SubscriptionView />
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
it("renders the API catalog, switches addons and shows an unavailable purchase notice", async () => {
  mount();
  await screen.findByRole("heading", { name: "月度会员" });
  expect(screen.getByText("19.90")).toBeTruthy();
  expect(screen.getByText("120 分钟语音转写")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "AI 次数包" }));
  expect(screen.queryByText("120 分钟语音转写")).toBeNull();
  expect(screen.getByText("100 次 AI 生成")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "加购说明" }));
  const dialog = await screen.findByRole("dialog", { name: "100 次 AI 包" });
  expect(within(dialog).getByText(/暂未开放在线购买/)).toBeTruthy();
  expect(within(dialog).queryByRole("button", { name: /支付/ })).toBeNull();
  fireEvent.click(within(dialog).getByRole("button", { name: "我知道了" }));
  expect(screen.queryByRole("dialog")).toBeNull();
});
it("does not invent catalog values after failure and supports retry", async () => {
  vi.mocked(api.catalog).mockRejectedValueOnce(
    new ApiError("NETWORK_ERROR", "加载失败"),
  );
  mount();
  await screen.findByText("加载失败");
  expect(screen.queryByText("19.90")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /重新加载/ }));
  expect(await screen.findByText("19.90")).toBeTruthy();
});
it("shows real balances, held usage and expiry without exposing the previous account", async () => {
  login();
  await billingStore.refresh();
  mount();
  expect(screen.getByText("29 分 59 秒")).toBeTruthy();
  expect(screen.getByText(/执行中占用 1 秒/)).toBeTruthy();
  fireEvent.click(screen.getByText("查看额度有效期"));
  expect(screen.getByText(/2099.*到期/)).toBeTruthy();
  act(() => authStore.clear());
  expect(screen.queryByText("29 分 59 秒")).toBeNull();
  expect(screen.getByRole("button", { name: "登录查看" })).toBeTruthy();
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
