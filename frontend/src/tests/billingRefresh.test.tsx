// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { useBillingRefresh } from "../hooks/useBillingRefresh";
import { authStore } from "../stores/auth";
import { billingStore, useBilling } from "../stores/billing";
import { allowances } from "./billingFixtures";

function AccountSnapshot() {
  useBillingRefresh();
  const { data } = useBilling();
  return <output>{data?.daily.downloads_used ?? "loading"}</output>;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-29T15:59:58Z"));
  billingStore.setUser(null);
  authStore.accept({
    status: "authenticated",
    access_token: "refresh-test",
    user: {
      id: "refresh-user",
      nickname: "用户",
      role: "user",
      status: "active",
      is_subscribed: true,
    },
  });
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  vi.spyOn(api, "allowances").mockResolvedValue(allowances);
});
afterEach(() => {
  cleanup();
  billingStore.setUser(null);
  authStore.clear();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it("coalesces initial requests but lets task settlement force an immediate refresh", async () => {
  let resolve!: (value: typeof allowances) => void;
  vi.mocked(api.allowances).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const first = billingStore.refresh({ ifStale: true });
  const second = billingStore.refresh({ ifStale: true });
  expect(api.allowances).toHaveBeenCalledOnce();
  expect(vi.mocked(api.allowances).mock.calls[0]![0]!.aborted).toBe(false);
  resolve(allowances);
  await Promise.all([first, second]);
  await billingStore.refresh({ ifStale: true });
  expect(api.allowances).toHaveBeenCalledOnce();
  await billingStore.refresh();
  expect(api.allowances).toHaveBeenCalledTimes(2);
});

it("refreshes the daily count at Beijing midnight without a page reload", async () => {
  vi.mocked(api.allowances)
    .mockResolvedValueOnce(allowances)
    .mockResolvedValue({
      ...allowances,
      daily: { ...allowances.daily, date: "2026-10-30", downloads_used: 0 },
    });
  render(<AccountSnapshot />);
  await act(async () => {});
  expect(screen.getByRole("status").textContent).toBe("1");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2051);
  });
  expect(api.allowances).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("status").textContent).toBe("0");
});

it.each(["membership", "allowance start", "allowance expiry"])(
  "refreshes at %s boundaries",
  async (boundary) => {
    vi.setSystemTime(new Date("2026-10-29T10:00:00Z"));
    const at = "2026-10-29T10:00:10Z";
    vi.mocked(api.allowances).mockResolvedValue({
      ...allowances,
      member_until: boundary === "membership" ? at : null,
      allowances: allowances.allowances.map((item) => ({
        ...item,
        starts_at: boundary === "allowance start" ? at : item.starts_at,
        expires_at: boundary === "allowance expiry" ? at : item.expires_at,
      })),
    });
    render(<AccountSnapshot />);
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_051);
    });
    expect(api.allowances).toHaveBeenCalledTimes(2);
  },
);

it("pauses hidden tabs and deduplicates visibility and focus refreshes on return", async () => {
  render(<AccountSnapshot />);
  await act(async () => {});
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  fireEvent(document, new Event("visibilitychange"));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(120_000);
  });
  expect(api.allowances).toHaveBeenCalledOnce();
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  await act(async () => {
    fireEvent(document, new Event("visibilitychange"));
    fireEvent(window, new Event("focus"));
  });
  expect(api.allowances).toHaveBeenCalledTimes(2);
});
