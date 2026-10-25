// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import HeroBackground from "../components/HeroBackground";

vi.mock("../components/AeroShards", () => ({
  default: ({
    paused,
    onError,
  }: {
    paused: boolean;
    onError: (error: Error) => void;
  }) => (
    <div data-testid="shards" data-paused={String(paused)}>
      <button
        data-testid="gpu-failure"
        onClick={() => onError(new Error("device lost"))}
      />
    </div>
  ),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function setupMedia(reduced = false, mobile = false) {
  vi.stubGlobal("navigator", { gpu: {} });
  const events = new EventTarget();
  const preference = {
    matches: reduced,
    addEventListener: (type: string, fn: EventListener) =>
      events.addEventListener(type, fn),
    removeEventListener: (type: string, fn: EventListener) =>
      events.removeEventListener(type, fn),
  };
  vi.stubGlobal("matchMedia", (query: string) =>
    query.includes("reduced-motion")
      ? preference
      : {
          matches: mobile,
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
        },
  );
  return { events, preference };
}

it("keeps a plain background without WebGPU", () => {
  vi.stubGlobal("navigator", {});
  const view = render(<HeroBackground />);
  expect(view.container.innerHTML).toBe("");
});

it.each([
  [true, false],
  [false, true],
])(
  "skips GPU decoration for reduced motion or mobile (%s, %s)",
  (reduced, mobile) => {
    setupMedia(reduced, mobile);
    const view = render(<HeroBackground />);
    expect(view.container.innerHTML).toBe("");
  },
);

it("unmounts on preference change and falls back after a GPU error", async () => {
  const { events, preference } = setupMedia();
  render(<HeroBackground />);
  await screen.findByTestId("shards");
  expect(screen.queryByRole("button", { name: "暂停背景动画" })).toBeNull();
  act(() => {
    preference.matches = true;
    events.dispatchEvent(new Event("change"));
  });
  expect(screen.queryByTestId("shards")).toBeNull();
  act(() => {
    preference.matches = false;
    events.dispatchEvent(new Event("change"));
  });
  await screen.findByTestId("shards");
  fireEvent.click(screen.getByTestId("gpu-failure"));
  await waitFor(() => expect(screen.queryByTestId("shards")).toBeNull());
  expect(screen.queryByRole("button")).toBeNull();
});
