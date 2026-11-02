// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { renderToString } from "react-dom/server";
import MarketingSite from "../marketing/MarketingSite";
import { publicDocument } from "../marketing/render";
import { api } from "../api";
import { authStore } from "../stores/auth";
import { documentPaths } from "../marketing/routes";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
const template =
  '<html lang="zh-CN"><head><title>App</title><meta name="description" content="app"></head><body><div id="app"></div></body></html>';

it("renders product facts and the case without making auth or business requests", () => {
  const login = vi.spyOn(authStore, "restore");
  const catalog = vi.spyOn(api, "catalog");
  const history = vi.spyOn(api, "history");
  render(<MarketingSite path="/" />);
  expect(
    screen.getByRole("heading", {
      level: 1,
      name: /每一帧，\s*皆有所得。/,
    }),
  ).toBeTruthy();
  expect(
    within(screen.getByRole("region", { name: /每一帧/ }))
      .getByRole("link", { name: "开始体验" })
      .getAttribute("href"),
  ).toBe("/app");
  const hero = within(screen.getByRole("region", { name: /每一帧/ }));
  expect(hero.getAllByRole("link")).toHaveLength(3);
  expect(
    hero.getByRole("link", { name: "看看实际处理结果" }).getAttribute("href"),
  ).toBe("#features");
  expect(
    hero.getByRole("link", { name: "查看示例" }).getAttribute("href"),
  ).toBe("/examples/knowledge-loop");
  expect(screen.getByText(/下载原片、检索文稿/)).toBeTruthy();
  expect(login).not.toHaveBeenCalled();
  expect(catalog).not.toHaveBeenCalled();
  expect(history).not.toHaveBeenCalled();
});

it("shows one source-backed example with real chapter timestamps and readable expandable map nodes", () => {
  const { container } = render(<MarketingSite path="/" />);
  expect(screen.getAllByRole("tablist")).toHaveLength(1);
  expect(screen.getAllByRole("tab")).toHaveLength(4);
  const chapterPanel = screen.getByRole("tabpanel", { name: "章节总结" });
  expect(
    [...chapterPanel.querySelectorAll("time")].map((time) => time.textContent),
  ).toEqual(["00:00", "00:16", "00:32"]);
  expect(
    within(chapterPanel)
      .getByRole("link", { name: "查看 32 秒处的章节原文" })
      .getAttribute("href"),
  ).toBe("/examples/knowledge-loop#segment-32");
  expect(
    screen.getByRole("link", { name: "查看完整处理记录" }).getAttribute("href"),
  ).toBe("/examples/knowledge-loop");
  fireEvent.click(screen.getByRole("tab", { name: "思维导图" }));
  const map = screen.getByRole("tabpanel", { name: "思维导图" });
  expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
  expect(chapterPanel.getAttribute("inert")).toBe("");
  expect(within(map).getByText("看视频之前")).toBeTruthy();
  expect(within(map).getByText("将文稿整理成章节")).toBeTruthy();
  const node = container.querySelector<HTMLDetailsElement>(
    ".m-showcase-branches details",
  )!;
  fireEvent.click(node.querySelector("summary")!);
  expect(node.open).toBe(true);
  expect(node.textContent).toContain("不要把每一句话都抄下来。");
  fireEvent.click(screen.getByRole("tab", { name: "章节总结" }));
  expect(node.open).toBe(false);
  expect(screen.queryByText("微信登录后处理，开始前确认额度。")).toBeNull();
});

it("lets visitors explore real outputs with keyboard tabs and a working sample download", () => {
  render(<MarketingSite path="/" />);
  const choices = within(screen.getByRole("tablist", { name: "探索产品能力" }));
  fireEvent.keyDown(choices.getByRole("tab", { name: /^章节总结/ }), {
    key: "Home",
  });
  const download = choices.getByRole("tab", { name: /^视频下载/ });
  expect(document.activeElement).toBe(download);
  expect(download.getAttribute("aria-selected")).toBe("true");
  expect(
    screen.getByRole("link", { name: /下载示例原片/ }).getAttribute("href"),
  ).toBe("/media/examples/knowledge-loop/source.mp4");
  fireEvent.keyDown(download, { key: "ArrowDown" });
  const transcript = screen.getByRole("tabpanel", { name: /^文稿提取/ });
  expect(
    within(transcript)
      .getByRole("link", { name: "查看 8 秒处原文" })
      .getAttribute("href"),
  ).toBe("/examples/knowledge-loop#segment-8");
  fireEvent.keyDown(choices.getByRole("tab", { name: /^文稿提取/ }), {
    key: "End",
  });
  expect(
    screen.getByRole("tabpanel", { name: /^思维导图/ }).textContent,
  ).toContain("让知识留下来");
  expect(
    choices.getByRole("tab", { name: /^思维导图/ }).getAttribute("tabindex"),
  ).toBe("0");
});

function motionEnvironment(reduced = false) {
  const callbacks: IntersectionObserverCallback[] = [];
  const observe = vi.fn();
  const unobserve = vi.fn();
  const disconnect = vi.fn();
  const changes: Array<() => void> = [];
  const preference = {
    matches: reduced,
    addEventListener: vi.fn((_name: string, callback: () => void) =>
      changes.push(callback),
    ),
    removeEventListener: vi.fn(),
  };
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) =>
      query.includes("reduced-motion") ? preference : { matches: false },
    ),
  );
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      constructor(callback: IntersectionObserverCallback) {
        callbacks.push(callback);
      }
      observe = observe;
      unobserve = unobserve;
      disconnect = disconnect;
    },
  );
  const bounds = vi
    .spyOn(HTMLElement.prototype, "getBoundingClientRect")
    .mockReturnValue({
      top: 1500,
      bottom: 1600,
      left: 0,
      right: 500,
      width: 500,
      height: 100,
      x: 0,
      y: 1500,
      toJSON: () => ({}),
    });
  return {
    callbacks,
    observe,
    unobserve,
    disconnect,
    preference,
    changes,
    bounds,
  };
}

function animationFrames() {
  let id = 0;
  const pending = new Map<number, FrameRequestCallback>();
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn((callback: FrameRequestCallback) => {
      pending.set(++id, callback);
      return id;
    }),
  );
  vi.stubGlobal(
    "cancelAnimationFrame",
    vi.fn((frame: number) => pending.delete(frame)),
  );
  return {
    pending,
    flush() {
      const callbacks = [...pending.values()];
      pending.clear();
      act(() => callbacks.forEach((callback) => callback(0)));
    },
  };
}

it("marks only the current navigation section while scrolling from the hero through content", () => {
  const motion = motionEnvironment();
  const frames = animationFrames();
  let offset = 0;
  motion.bounds.mockImplementation(function (this: HTMLElement) {
    const starts: Record<string, number> = {
      features: 900,
      "how-it-works": 1600,
      about: 2200,
      faq: 2800,
    };
    const top =
      (this.classList.contains("m-hero") ? 0 : (starts[this.id] ?? 4000)) -
      offset;
    return {
      left: 0,
      top,
      width: 500,
      height: 400,
      right: 500,
      bottom: top + 400,
      x: 0,
      y: top,
      toJSON: () => ({}),
    };
  });
  const { unmount } = render(<MarketingSite path="/" />);
  const nav = screen.getByRole("navigation", { name: "主导航" });
  const current = (name: string) => {
    expect(nav.querySelectorAll('[aria-current="location"]')).toHaveLength(1);
    expect(
      within(nav).getByRole("link", { name }).getAttribute("aria-current"),
    ).toBe("location");
  };
  expect(nav.querySelector("[aria-current]")).toBeNull();
  for (const [position, name] of [
    [700, "功能介绍"],
    [1400, "使用流程"],
    [2600, "常见问题"],
  ] as const) {
    offset = position;
    fireEvent.scroll(window);
    frames.flush();
    current(name);
  }
  unmount();
  expect(nav.querySelector('[aria-current="location"]')).toBeNull();
});

it("keeps the side-card curves attached after resizing even with reduced motion", () => {
  const motion = motionEnvironment(true);
  const frames = animationFrames();
  let wider = false;
  motion.bounds.mockImplementation(function (this: HTMLElement) {
    let left = 0,
      top = 0,
      width = wider ? 1320 : 1063,
      height = 804;
    if (this.classList.contains("m-hero-stage")) {
      left = wider ? 260 : 180;
      top = 360;
      width = wider ? 800 : 704;
      height = 428;
    } else if (this.classList.contains("m-hero-side-left")) {
      left = 40;
      top = 250;
      width = 145;
      height = 160;
    } else if (this.classList.contains("m-hero-side-right")) {
      left = wider ? 1130 : 880;
      top = 340;
      width = 145;
      height = 160;
    }
    return {
      left,
      top,
      width,
      height,
      right: left + width,
      bottom: top + height,
      x: left,
      y: top,
      toJSON: () => ({}),
    };
  });
  const { container, unmount } = render(<MarketingSite path="/" />);
  const path = container.querySelector('[data-hero-connector="left"]')!;
  const points = () =>
    path
      .getAttribute("d")!
      .match(/-?\d+(?:\.\d+)?/g)!
      .map(Number);
  const original = points();
  expect(original[0]).toBeGreaterThan(40);
  expect(original[0]).toBeLessThan(185);
  expect(original[1]).toBeGreaterThan(250);
  expect(original[1]).toBeLessThan(410);
  expect(original.at(-2)).toBeGreaterThan(180);
  expect(original.at(-2)).toBeLessThan(884);
  wider = true;
  fireEvent.resize(window);
  frames.flush();
  expect(points().at(-2)).toBeGreaterThan(original.at(-2)!);
  expect(
    container.querySelector(".m-landing-site")?.getAttribute("data-motion"),
  ).toBeNull();
  fireEvent.resize(window);
  unmount();
  expect(frames.pending.size).toBe(0);
});

it("batches hero scroll motion, bounds displacement and removes it when reduced motion is enabled", () => {
  const motion = motionEnvironment();
  const frames = animationFrames();
  let top = 24;
  motion.bounds.mockImplementation(function (this: HTMLElement) {
    const y = this.classList.contains("m-hero") ? top : 1500;
    return {
      top: y,
      bottom: y + 1000,
      left: 0,
      right: 500,
      width: 500,
      height: 1000,
      x: 0,
      y,
      toJSON: () => ({}),
    };
  });
  const { container, unmount } = render(<MarketingSite path="/" />);
  const hero = container.querySelector<HTMLElement>(".m-hero")!;
  expect(hero.style.getPropertyValue("--m-hero-spread")).toBe("0px");
  top = -425;
  fireEvent.scroll(window);
  fireEvent.scroll(window);
  // One queued frame for navigation and one for hero layers, independent of event frequency.
  expect(frames.pending.size).toBe(2);
  frames.flush();
  expect(hero.style.getPropertyValue("--m-hero-spread")).toBe("25px");
  expect(hero.style.getPropertyValue("--m-hero-copy-y")).toBe("-20px");
  top = -5000;
  fireEvent.scroll(window);
  frames.flush();
  expect(hero.style.getPropertyValue("--m-hero-spread")).toBe("50px");
  fireEvent.scroll(window);
  act(() => {
    motion.preference.matches = true;
    motion.changes.forEach((change) => change());
  });
  expect(hero.style.getPropertyValue("--m-hero-spread")).toBe("");
  frames.flush();
  expect(hero.style.getPropertyValue("--m-hero-copy-y")).toBe("");
  unmount();
  expect(frames.pending.size).toBe(0);
});

it("applies mouse parallax only on fine-pointer devices and clears pending motion on exit", () => {
  const motion = motionEnvironment();
  const frames = animationFrames();
  vi.mocked(window.matchMedia).mockImplementation(
    (query) =>
      (query.includes("reduced-motion")
        ? motion.preference
        : { matches: query.includes("pointer: fine") }) as MediaQueryList,
  );
  motion.bounds.mockReturnValue({
    top: 0,
    bottom: 500,
    left: 0,
    right: 500,
    width: 500,
    height: 500,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  });
  const { container, unmount } = render(<MarketingSite path="/" />);
  const hero = container.querySelector<HTMLElement>(".m-hero")!;
  const stage = container.querySelector<HTMLElement>(".m-hero-stage")!;
  const move = (pointerType: string) => {
    const event = new Event("pointermove");
    Object.defineProperties(event, {
      pointerType: { value: pointerType },
      clientX: { value: 500 },
      clientY: { value: 250 },
    });
    fireEvent(hero, event);
  };
  move("touch");
  expect(frames.pending.size).toBe(0);
  move("mouse");
  frames.flush();
  expect(stage.style.getPropertyValue("--m-hero-pointer-x")).toBe("10px");
  move("mouse");
  fireEvent.pointerLeave(hero);
  expect(stage.style.getPropertyValue("--m-hero-pointer-x")).toBe("");
  expect(frames.pending.size).toBe(0);
  unmount();
});

it("reveals below-fold content once, exposes keyboard targets and cleans up observers", () => {
  const motion = motionEnvironment();
  const { container, unmount } = render(<MarketingSite path="/" />);
  const root = container.querySelector<HTMLElement>(".m-landing-site")!;
  const target = container.querySelector<HTMLElement>(".m-showcase-heading")!;
  expect(target.dataset.revealed).toBe("false");
  expect(root.dataset.motion).toBe("ready");
  act(() =>
    motion.callbacks[0](
      [
        {
          target,
          isIntersecting: true,
        } as unknown as IntersectionObserverEntry,
      ],
      {} as IntersectionObserver,
    ),
  );
  expect(target.dataset.revealed).toBe("true");
  expect(motion.unobserve).toHaveBeenCalledWith(target);
  const feature = container.querySelector<HTMLElement>(".m-showcase-window")!;
  expect(feature.dataset.revealed).toBe("false");
  act(() => screen.getByRole("tab", { name: /^视频下载/ }).focus());
  expect(feature.dataset.revealed).toBe("true");
  unmount();
  expect(motion.disconnect).toHaveBeenCalled();
  expect(root.dataset.motion).toBeUndefined();
});

it("keeps content visible for reduced motion and when the preference changes", () => {
  const motion = motionEnvironment(true);
  const { container } = render(<MarketingSite path="/" />);
  const root = container.querySelector<HTMLElement>(".m-landing-site")!;
  expect(root.dataset.motion).toBeUndefined();
  expect(container.querySelector('[data-revealed="false"]')).toBeNull();
  expect(motion.observe).not.toHaveBeenCalled();
  act(() => {
    motion.preference.matches = false;
    motion.changes.forEach((change) => change());
  });
  expect(root.dataset.motion).toBe("ready");
  act(() => {
    motion.preference.matches = true;
    motion.changes.forEach((change) => change());
  });
  expect(root.dataset.motion).toBeUndefined();
  expect(container.querySelector('[data-revealed="false"]')).toBeNull();
  expect(motion.disconnect).toHaveBeenCalled();
});

it("ships readable static content without motion-dependent visibility or running effects on other pages", () => {
  const html = renderToString(<MarketingSite path="/" />);
  expect(html).not.toContain('data-revealed="false"');
  expect(html).not.toContain('data-motion="ready"');
  const initial = new DOMParser().parseFromString(html, "text/html");
  const pieces = initial.querySelectorAll<HTMLElement>("[data-fold-piece]");
  expect(pieces.length).toBeGreaterThan(0);
  pieces.forEach((piece) => {
    expect(piece.style.opacity).not.toBe("0");
    expect(piece.style.transform).toBe("");
  });
  expect(initial.querySelector(".m-gooey-nav [aria-current]")).toBeNull();
  expect(initial.getElementById("example")).toBeTruthy();
  const motion = motionEnvironment();
  render(<MarketingSite path="/help" />);
  expect(motion.observe).not.toHaveBeenCalled();
});

it("keeps the complete case text, provenance and outputs available without client JavaScript", () => {
  const html = renderToString(
    <MarketingSite path="/examples/knowledge-loop" />,
  );
  expect(html).toContain("heuristic-v1");
  expect(html).toContain("不代表线上云端模型");
  expect(html).toContain('id="segment-40"');
  expect(html).toContain("source.mp4");
  expect(html).toContain("result.json");
  expect(html).toContain("真正留下的知识");
});

it.each(documentPaths)(
  "publishes independent metadata, canonical and readable HTML for %s",
  (path) => {
    const html = publicDocument(template, path, { styles: ["/styles.css"] });
    const document = new DOMParser().parseFromString(html, "text/html");
    expect(document.querySelectorAll("h1")).toHaveLength(1);
    expect(
      document.querySelector('meta[name="robots"]')?.getAttribute("content"),
    ).toBe("index,follow");
    expect(
      document.querySelector('link[rel="canonical"]')?.getAttribute("href"),
    ).toBe(`https://vedio.vip${path}`);
    expect(document.querySelectorAll('meta[name="description"]')).toHaveLength(
      1,
    );
    expect(
      document.querySelector('script[type="application/ld+json"]'),
    ).toBeTruthy();
  },
);

it("keeps private shells and missing pages noindex without a homepage redirect or canonical", () => {
  for (const [path, empty] of [
    ["/account", true],
    ["/missing", false],
  ] as const) {
    const html = publicDocument(template, path, { styles: [], empty });
    expect(html).toContain('content="noindex,follow"');
    expect(html).not.toContain('rel="canonical"');
    expect(html).not.toContain('http-equiv="refresh"');
  }
  expect(
    publicDocument(template, "/", { styles: [], indexing: false }),
  ).toContain('content="noindex,follow"');
});
