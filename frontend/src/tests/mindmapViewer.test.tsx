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
import MindMapViewer from "../components/MindMapViewer";
import type { MindMapResult } from "../types/api";
import { downloadText } from "../utils";

const mocks = vi.hoisted(() => ({ create: vi.fn(), html: vi.fn() }));
vi.mock("../lib/mindmapRenderer", () => ({ createMindmap: mocks.create }));
vi.mock("markmap-lib", () => ({
  Transformer: class {
    md = { set: mocks.html };
    transform = () => ({ root: { content: "主题", children: [] } });
  },
}));
vi.mock("../utils", async (original) => ({
  ...(await original<typeof import("../utils")>()),
  downloadText: vi.fn(),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});
const result = {
  id: "map-1",
  result_type: "mindmap",
  markdown_export: "# 主题\n## 要点",
  content_json: { title: "主题", children: [] },
} as unknown as MindMapResult;

function setup() {
  vi.spyOn(SVGElement.prototype, "getBoundingClientRect").mockReturnValue({
    width: 640,
    height: 480,
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    bottom: 480,
    right: 640,
    toJSON: () => ({}),
  });
  const observers: Array<() => void> = [];
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: () => void) {
        observers.push(callback);
      }
      observe() {}
      disconnect() {}
    },
  );
  const engine = {
    setData: vi.fn().mockResolvedValue(undefined),
    renderData: vi.fn().mockResolvedValue(undefined),
    fit: vi.fn().mockResolvedValue(undefined),
    rescale: vi.fn().mockResolvedValue(undefined),
    destroy: vi.fn(),
    state: { rect: { x1: 0, y1: 0, x2: 320, y2: 240 } },
  };
  mocks.create.mockImplementation(() => engine);
  return { engine, observers };
}

it("waits for render completion before export, retains controls and disables Markdown HTML", async () => {
  const { engine, observers } = setup();
  let resolve!: () => void;
  engine.setData.mockImplementation(
    () =>
      new Promise<void>((done) => {
        resolve = done;
      }),
  );
  render(<MindMapViewer result={result} />);
  expect(
    (screen.getByTitle("下载 SVG 矢量图") as HTMLButtonElement).disabled,
  ).toBe(true);
  await waitFor(() => expect(engine.setData).toHaveBeenCalled());
  act(() => observers[0]());
  expect(engine.fit).not.toHaveBeenCalled();
  await act(async () => resolve());
  await waitFor(() =>
    expect(
      (screen.getByTitle("下载 SVG 矢量图") as HTMLButtonElement).disabled,
    ).toBe(false),
  );
  expect(mocks.html).toHaveBeenCalledWith({ html: false });
  fireEvent.click(screen.getByRole("button", { name: "放大" }));
  expect(engine.rescale).toHaveBeenCalledWith(1.25);
  fireEvent.click(screen.getByRole("button", { name: "缩小" }));
  expect(engine.rescale).toHaveBeenCalledWith(0.8);
  fireEvent.click(screen.getByRole("button", { name: "适应画布" }));
  expect(engine.fit).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByTitle("下载 SVG 矢量图"));
  await waitFor(() =>
    expect(downloadText).toHaveBeenCalledWith(
      expect.stringContaining('viewBox="-32 -32 384 304"'),
      "videohub-mindmap.svg",
      "image/svg+xml;charset=utf-8",
    ),
  );
});

it("keeps Markdown available after rendering fails and recovers on retry", async () => {
  const { engine } = setup();
  engine.setData.mockRejectedValueOnce(new Error("layout failed"));
  render(<MindMapViewer result={result} />);
  await screen.findByText(/导图暂时无法渲染/);
  fireEvent.click(screen.getByTitle("下载 Markdown 文件"));
  expect(downloadText).toHaveBeenCalledWith(
    result.markdown_export,
    "videohub-mindmap.md",
    "text/markdown;charset=utf-8",
  );
  fireEvent.click(screen.getByRole("button", { name: "重新渲染" }));
  await waitFor(() =>
    expect(
      (screen.getByTitle("下载 SVG 矢量图") as HTMLButtonElement).disabled,
    ).toBe(false),
  );
  expect(engine.destroy).toHaveBeenCalled();
});

it("destroys the engine and ignores a late initial render after unmount", async () => {
  const { engine } = setup();
  let resolve!: () => void;
  engine.setData.mockImplementation(
    () =>
      new Promise<void>((done) => {
        resolve = done;
      }),
  );
  const view = render(<MindMapViewer result={result} />);
  await waitFor(() => expect(engine.setData).toHaveBeenCalled());
  view.unmount();
  await act(async () => resolve());
  expect(engine.destroy).toHaveBeenCalledOnce();
  expect(engine.fit).not.toHaveBeenCalled();
});
