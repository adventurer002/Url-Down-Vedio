// @vitest-environment jsdom
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import FoldText from "../marketing/components/FoldText";

const engine = vi.hoisted(() => ({
  timeline: vi.fn(),
  fromTo: vi.fn(),
  kill: vi.fn(),
}));
vi.mock("gsap", () => ({
  gsap: {
    parseEase: () => (value: number) => value,
    timeline: engine.timeline,
    ticker: { add: vi.fn(), remove: vi.fn() },
    killTweensOf: vi.fn(),
  },
}));

let now = 100;
beforeEach(() => {
  now = 100;
  vi.stubGlobal("performance", {
    now: () => now,
    getEntriesByType: () => [
      { name: "first-contentful-paint", startTime: 100 },
    ],
  });
  engine.timeline.mockReturnValue({ fromTo: engine.fromTo, kill: engine.kill });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

it("starts the entry fold when the engine is ready promptly", async () => {
  render(<FoldText text="每一帧" />);
  await act(async () => {
    await import("gsap");
  });
  expect(engine.timeline).toHaveBeenCalledOnce();
  expect(engine.fromTo).toHaveBeenCalledOnce();
});

it("keeps readable text unchanged when the engine arrives after the entry window", async () => {
  const { container } = render(<FoldText text="每一帧" />);
  now = 3100;
  await act(async () => {
    await import("gsap");
  });
  expect(engine.timeline).not.toHaveBeenCalled();
  container
    .querySelectorAll<HTMLElement>("[data-fold-piece]")
    .forEach((piece) => {
      expect(piece.style.opacity).toBe("");
      expect(piece.style.transform).toBe("");
    });
});

it("does not replay server text when hydration itself happens late", async () => {
  now = 2000;
  render(<FoldText text="每一帧" />);
  await act(async () => {
    await import("gsap");
  });
  expect(engine.timeline).not.toHaveBeenCalled();
});
