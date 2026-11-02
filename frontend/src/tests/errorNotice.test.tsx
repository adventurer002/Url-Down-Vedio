// @vitest-environment jsdom
import { StrictMode } from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ErrorNotice from "../components/ErrorNotice";
import { ApiError } from "../api/client";
const failure = () =>
  new ApiError(
    "DATABASE_UNAVAILABLE",
    "任务数据库暂不可用，请稍后重试",
    "req_notice_test",
  );
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
it("escapes the page stacking context and can be dismissed without removing form input", () => {
  const dismiss = vi.fn();
  const view = render(
    <section style={{ isolation: "isolate", transform: "translateZ(0)" }}>
      <input defaultValue="recording.mp3" aria-label="文件名" />
      <ErrorNotice error={failure()} id="upload-error" onDismiss={dismiss} />
    </section>,
  );
  expect(view.container.querySelector('[role="alert"]')).toBeNull();
  expect(
    screen.getByRole("alert").closest(".notice-layer")?.parentElement,
  ).toBe(document.body);
  expect(screen.getByText("查看详情").closest("details")!.open).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "关闭错误提示" }));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(dismiss).toHaveBeenCalledOnce();
  expect(document.querySelector(".notice-layer")).toBeNull();
  expect(
    (screen.getByRole("textbox", { name: "文件名" }) as HTMLInputElement).value,
  ).toBe("recording.mp3");
});
it("keeps a dismissed occurrence closed on rerender and shows the next failed attempt with identical text", () => {
  const error = failure();
  const view = render(<ErrorNotice error={error} />);
  fireEvent.click(screen.getByRole("button", { name: "关闭错误提示" }));
  view.rerender(<ErrorNotice error={error} />);
  expect(screen.queryByRole("alert")).toBeNull();
  view.rerender(<ErrorNotice error={failure()} />);
  expect(screen.getByRole("alert")).toBeTruthy();
  view.rerender(<ErrorNotice error={null} />);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(document.querySelector(".notice-layer")).toBeNull();
});
it("stacks concurrent errors in one shared layer and cleans up during Strict Mode remounts", () => {
  const a = new ApiError("FIRST_ERROR", "第一次失败"),
    b = new ApiError("SECOND_ERROR", "第二次失败");
  const view = render(
    <StrictMode>
      <ErrorNotice error={a} />
      <ErrorNotice error={b} />
    </StrictMode>,
  );
  expect(document.querySelectorAll(".notice-layer")).toHaveLength(1);
  expect(screen.getAllByRole("alert")).toHaveLength(2);
  fireEvent.click(
    within(screen.getAllByRole("alert")[0]!).getByRole("button", {
      name: "关闭错误提示",
    }),
  );
  expect(screen.getAllByRole("alert")).toHaveLength(1);
  expect(screen.getByText("第二次失败")).toBeTruthy();
  view.unmount();
  expect(document.querySelectorAll(".notice-layer")).toHaveLength(0);
});
it("keeps modal errors inside the modal subtree so dismiss and retry controls remain interactive", () => {
  const retry = vi.fn();
  const view = render(
    <dialog open aria-label="确认操作">
      <ErrorNotice error={failure()}>
        <button onClick={retry}>重试请求</button>
      </ErrorNotice>
    </dialog>,
  );
  const modal = screen.getByRole("dialog");
  expect(within(modal).getByRole("alert")).toBeTruthy();
  fireEvent.click(within(modal).getByRole("button", { name: "重试请求" }));
  expect(retry).toHaveBeenCalledOnce();
  fireEvent.click(within(modal).getByRole("button", { name: "关闭错误提示" }));
  expect(within(modal).queryByRole("alert")).toBeNull();
  expect(modal.hasAttribute("open")).toBe(true);
  view.unmount();
});
it("waits for a containing dialog to open before promoting the notice to the top layer", async () => {
  const show = vi.fn();
  Object.defineProperty(HTMLElement.prototype, "showPopover", {
    configurable: true,
    value: show,
  });
  try {
    const view = render(
      <dialog aria-label="延迟打开">
        <ErrorNotice error={failure()} />
      </dialog>,
    );
    expect(show).not.toHaveBeenCalled();
    view.container.querySelector("dialog")!.setAttribute("open", "");
    await waitFor(() => expect(show).toHaveBeenCalledOnce());
    view.unmount();
  } finally {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>)
      .showPopover;
  }
});
