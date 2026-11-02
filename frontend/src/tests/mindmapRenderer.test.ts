// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createMindmap } from "../lib/mindmapRenderer";
import {
  createBranchTheme,
  orthogonalPath,
  serializeMindmapSvg,
  type MapNode,
} from "../lib/mindmapTheme";

const maps: ReturnType<typeof createMindmap>[] = [];
const rect = (width = 900, height = 560) => ({
  x: 0,
  y: 0,
  top: 0,
  left: 0,
  right: width,
  bottom: height,
  width,
  height,
  toJSON: () => ({}),
});

beforeEach(() => {
  // jsdom has no font measurement or SVG matrices. Supply deterministic geometry;
  // layout, folding, D3 updates and SVG serialization all use the real renderer.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(
    function (this: HTMLElement) {
      return Math.min(250, (this.textContent?.length || 1) * 14 + 26);
    },
  );
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(
    function (this: HTMLElement) {
      return (
        Math.ceil(((this.textContent?.length || 1) * 14 + 26) / 250) * 26 + 18
      );
    },
  );
  const properties: Record<string, PropertyDescriptor> = {
    transform: {
      configurable: true,
      get(this: SVGElement) {
        return {
          baseVal: {
            consolidate: () => {
              const text = this.getAttribute("transform") || "";
              const position = /translate\(([-\d.]+)[ ,]([-\d.]+)\)/.exec(text);
              const scale = Number(/scale\(([-\d.]+)\)/.exec(text)?.[1] || 1);
              return {
                matrix: {
                  a: scale,
                  b: 0,
                  c: 0,
                  d: scale,
                  e: Number(position?.[1] || 0),
                  f: Number(position?.[2] || 0),
                },
              };
            },
          },
        };
      },
    },
    viewBox: {
      configurable: true,
      get(this: SVGElement) {
        const [x, y, width, height] = (
          this.getAttribute("viewBox") || "0 0 900 560"
        )
          .split(" ")
          .map(Number);
        return { baseVal: { x, y, width, height } };
      },
    },
  };
  Object.defineProperties(SVGElement.prototype, properties);
});
afterEach(() => {
  maps.forEach((map) => map.destroy());
  maps.length = 0;
  document.body.replaceChildren();
  delete (SVGElement.prototype as { transform?: unknown }).transform;
  delete (SVGElement.prototype as { viewBox?: unknown }).viewBox;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function canvas() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "mindmap-svg videohub-mindmap");
  svg.setAttribute("viewBox", "0 0 900 560");
  svg.getBoundingClientRect = () => rect();
  document.body.append(svg);
  const onError = vi.fn();
  const map = createMindmap(svg, onError);
  maps.push(map);
  return { svg, map, onError };
}
const leaf = (content: string) => ({ content, children: [] });
const tree = () => ({
  content: "视频知识地图",
  children: [
    {
      content: "核心概念",
      children: [
        { content: "概念的定义", children: [leaf("对照原文核对定义")] },
        leaf("概念之间的关系"),
      ],
    },
    {
      content: "实践方法",
      children: [leaf("第一步：提出问题"), leaf("第二步：核对来源")],
    },
    { content: "复习计划", children: [leaf("记录不确定的地方")] },
  ],
});
function assertStraight(svg: SVGSVGElement) {
  const links = [...svg.querySelectorAll("path.markmap-link")];
  expect(links.length).toBeGreaterThan(0);
  for (const link of links) {
    expect(link.getAttribute("d")).toMatch(
      /^M[-\d.]+,[-\d.]+H[-\d.]+V[-\d.]+H[-\d.]+$/,
    );
    expect(link.getAttribute("stroke")).toMatch(/^#[a-f\d]{6}$/);
  }
}

it("keeps branch hues stable across depth and branch changes", () => {
  const theme = createBranchTheme();
  const node = (path: string) =>
    ({ state: { path, depth: path.split(".").length } }) as MapNode;
  const blue = theme(node("1.2"));
  expect(theme(node("1.2.3.4"))).toBe(blue);
  const green = theme(node("1.7"));
  expect(green.color).not.toBe(blue.color);
  expect(theme(node("1.2"))).toBe(blue);
  expect(theme(node("1")).background).toBe("#1f2937");
});

it("connects card edges at their vertical centers with right-angle segments", () => {
  const node = (x: number, y: number, width: number, height: number) =>
    ({ state: { rect: { x, y, width, height } } }) as MapNode;
  expect(
    orthogonalPath(
      { source: node(0, 0, 120, 40), target: node(184, 80, 200, 60) },
      12,
    ),
  ).toBe("M108,20H152V110H196");
});

it("uses the real layout and preserves straight colored links through fold/unfold and resize", async () => {
  const { svg, map, onError } = canvas();
  await map.setData(tree());
  await map.fit();
  assertStraight(svg);
  const first = map.state.data!.children[0];
  const color = map.options.color(first);
  expect(map.options.color(first.children[0])).toBe(color);
  expect(map.options.color(map.state.data!.children[1])).not.toBe(color);
  const count = svg.querySelectorAll("path.markmap-link").length;
  await map.toggleNode(first);
  expect(svg.querySelectorAll("path.markmap-link").length).toBeLessThan(count);
  assertStraight(svg);
  await map.toggleNode(first);
  await map.toggleNode(first.children[0]);
  assertStraight(svg);
  expect(map.options.color(first.children[0].children[0])).toBe(color);
  svg.getBoundingClientRect = () => rect(375, 480);
  await map.fit();
  await map.rescale(1.25);
  await map.renderData();
  await map.fit();
  assertStraight(svg);
  expect(svg.querySelector('g[data-depth="4"]')).not.toBeNull();
  expect(svg.querySelector("style")?.textContent).toContain("font-size:20px");
  expect(onError).not.toHaveBeenCalled();
});

it("exports all currently expanded content and its colors independent of screen zoom", async () => {
  const { svg, map } = canvas();
  await map.setData(tree());
  await map.fit();
  await map.rescale(2);
  const screenTransform = svg
    .querySelector(".videohub-mindmap-canvas")!
    .getAttribute("transform");
  const exported = serializeMindmapSvg(svg, map.state.rect);
  const doc = new DOMParser().parseFromString(exported, "image/svg+xml");
  expect(doc.querySelector("parsererror")).toBeNull();
  expect(
    doc.querySelector(".videohub-mindmap-canvas")!.getAttribute("transform"),
  ).toBeNull();
  expect(
    svg.querySelector(".videohub-mindmap-canvas")!.getAttribute("transform"),
  ).toBe(screenTransform);
  expect(doc.documentElement.getAttribute("viewBox")).not.toBe("0 0 900 560");
  expect(doc.querySelector("rect")!.getAttribute("fill")).toBe("#ffffff");
  expect(doc.querySelector("style")!.textContent).toContain(
    ".videohub-mindmap",
  );
  expect(
    [...doc.querySelectorAll('g.markmap-node[data-depth="2"]')].map((node) =>
      (node as SVGElement).style.getPropertyValue("--mm-color"),
    ),
  ).toEqual(["#2563eb", "#047857", "#c2410c"]);
  for (const path of doc.querySelectorAll("path.markmap-link"))
    expect(path.getAttribute("d")).not.toMatch(/[CQSA]/i);
});

it("renders single roots and long multiline maps with finite layout and cleans up pending renders", async () => {
  const { svg, map } = canvas();
  await map.setData(leaf("只有一个中心主题"));
  await map.fit();
  expect(svg.querySelectorAll("g.markmap-node")).toHaveLength(1);
  expect(svg.querySelectorAll("path.markmap-link")).toHaveLength(0);
  await map.setData({
    content: "长内容",
    children: Array.from({ length: 24 }, (_, i) => ({
      content: `章节 ${i + 1}`,
      children: [
        leaf(
          "一个很长的节点标题，用于验证自动换行后节点之间仍保留足够间距。".repeat(
            3,
          ),
        ),
      ],
    })),
  });
  await map.fit();
  assertStraight(svg);
  expect(Object.values(map.state.rect).every(Number.isFinite)).toBe(true);
  const pending = map.renderData();
  map.destroy();
  await pending;
  await map.renderData();
  expect(svg.childElementCount).toBe(0);
});

it("cancels queued canvas animations when the map is destroyed", async () => {
  const { svg, map } = canvas();
  await map.setData(tree());
  const started = vi.fn();
  map.g.transition().duration(0).on("start.lifecycle", started);
  map.destroy();
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(started).not.toHaveBeenCalled();
  expect(svg.childElementCount).toBe(0);
});
