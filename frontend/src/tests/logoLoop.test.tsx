// @vitest-environment jsdom
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PlatformLogos from "../components/PlatformLogos";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("presents one accessible platform list without extra captions or controls", () => {
  render(<PlatformLogos />);
  const loop = screen.getByRole("region", { name: "视频来源平台" });
  expect(within(loop).getAllByRole("listitem")).toHaveLength(5);
  expect(screen.queryByText("常见视频来源")).toBeNull();
  expect(screen.queryByRole("button")).toBeNull();
});

it("switches to a complete static list when reduced motion is enabled and removes its listener", () => {
  const events = new EventTarget();
  const media = {
    matches: true,
    addEventListener: vi.fn((type: string, callback: EventListener) =>
      events.addEventListener(type, callback),
    ),
    removeEventListener: vi.fn((type: string, callback: EventListener) =>
      events.removeEventListener(type, callback),
    ),
  };
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => media),
  );
  const view = render(<PlatformLogos />);
  const list = screen.getByRole("list", { name: "视频来源平台" });
  expect(within(list).getAllByRole("listitem")).toHaveLength(5);
  for (const name of ["YouTube", "bilibili", "Vimeo", "Dailymotion", "TikTok"])
    expect(within(list).getByText(name)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "暂停平台标识滚动" })).toBeNull();
  act(() => {
    media.matches = false;
    events.dispatchEvent(new Event("change"));
  });
  expect(screen.getByRole("region", { name: "视频来源平台" })).toBeTruthy();
  view.unmount();
  expect(media.removeEventListener).toHaveBeenCalledWith(
    "change",
    expect.any(Function),
  );
});
