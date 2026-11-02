// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ClearHistoryConfirm from "../components/ClearHistoryConfirm";
import { ApiError } from "../api/client";

beforeEach(() => {
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
  vi.restoreAllMocks();
});

it("focuses the safe action and allows cancellation without clearing history", () => {
  const onConfirm = vi.fn(),
    onClose = vi.fn();
  render(
    <ClearHistoryConfirm count={32} onConfirm={onConfirm} onClose={onClose} />,
  );
  expect(screen.getByText(/当前账号共有 32 条/)).toBeTruthy();
  expect(document.activeElement).toBe(
    screen.getByRole("button", { name: "保留记录" }),
  );
  fireEvent(
    screen.getByRole("dialog"),
    new Event("cancel", { cancelable: true }),
  );
  expect(onClose).toHaveBeenCalledOnce();
  expect(onConfirm).not.toHaveBeenCalled();
});

it("blocks duplicate clearing, keeps failures visible and closes only after success", async () => {
  let reject!: (error: unknown) => void;
  const onConfirm = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise((_, fail) => {
          reject = fail;
        }),
    )
    .mockResolvedValue(undefined);
  const onClose = vi.fn();
  render(
    <ClearHistoryConfirm count={5} onConfirm={onConfirm} onClose={onClose} />,
  );
  const confirm = screen.getByRole("button", { name: "确认隐藏记录" });
  fireEvent.click(confirm);
  fireEvent.click(confirm);
  expect(onConfirm).toHaveBeenCalledOnce();
  fireEvent(
    screen.getByRole("dialog"),
    new Event("cancel", { cancelable: true }),
  );
  expect(onClose).not.toHaveBeenCalled();
  reject(new ApiError("NETWORK_ERROR", "网络暂不可用"));
  expect(await screen.findByText("网络暂不可用")).toBeTruthy();
  expect(onClose).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认隐藏记录" }));
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  expect(onConfirm).toHaveBeenCalledTimes(2);
});
