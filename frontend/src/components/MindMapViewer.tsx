import { useEffect, useRef, useState } from "react";
import type { Markmap } from "markmap-view";
import type { MindMapResult } from "../types/api";
import { treeToMarkdown, downloadText } from "../utils";
import { serializeMindmapSvg } from "../lib/mindmapTheme";
import Icon from "./Icon";
export default function MindMapViewer({ result }: { result: MindMapResult }) {
  const svg = useRef<SVGSVGElement>(null),
    map = useRef<Markmap | undefined>(undefined);
  const [error, setError] = useState(""),
    [loading, setLoading] = useState(false),
    [ready, setReady] = useState(false),
    [attempt, setAttempt] = useState(0);
  const markdown =
    result.markdown_export || treeToMarkdown(result.content_json);
  function sizeCanvas() {
    const element = svg.current;
    if (!element) return false;
    const box = element.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return false;
    element.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
    element.setAttribute("width", String(box.width));
    element.setAttribute("height", String(box.height));
    return true;
  }
  useEffect(() => {
    let disposed = false,
      rendering = false,
      rendered = false;
    setReady(false);
    async function render() {
      if (disposed || rendering || !sizeCanvas()) return;
      rendering = true;
      setLoading(true);
      setError("");
      try {
        const [{ Transformer }, { createMindmap }] = await Promise.all([
          import("markmap-lib"),
          import("../lib/mindmapRenderer"),
        ]);
        if (disposed || !svg.current || !sizeCanvas()) return;
        map.current?.destroy();
        const transformer = new Transformer();
        transformer.md.set({ html: false });
        const { root } = transformer.transform(markdown);
        const instance = createMindmap(svg.current, () => {
          if (!disposed) setError("节点调整失败，请重新渲染。");
        });
        map.current = instance;
        await instance.setData(root);
        if (disposed || map.current !== instance) return;
        await instance.fit();
        if (!disposed) {
          rendered = true;
          setReady(true);
        }
      } catch {
        if (!disposed)
          setError("导图暂时无法渲染，请重试；仍可导出 Markdown。");
      } finally {
        rendering = false;
        if (!disposed) setLoading(false);
      }
    }
    const observer = new ResizeObserver(() => {
      if (disposed || rendering || !sizeCanvas()) return;
      if (map.current && rendered)
        void map.current.fit().catch(() => {
          if (!disposed) setError("画布调整失败，请重新渲染。");
        });
      else void render();
    });
    if (svg.current) observer.observe(svg.current);
    void render();
    return () => {
      disposed = true;
      observer.disconnect();
      map.current?.destroy();
      map.current = undefined;
    };
  }, [result.id, markdown, attempt]);
  async function adjust(scale?: number) {
    if (!sizeCanvas() || !map.current) return;
    try {
      if (scale) await map.current.rescale(scale);
      else await map.current.fit();
    } catch {
      setError("画布调整失败，请重新渲染。");
    }
  }
  async function exportSvg() {
    if (!svg.current || !map.current || !ready) return;
    const instance = map.current;
    try {
      await instance.renderData();
      if (!svg.current || map.current !== instance) return;
      downloadText(
        serializeMindmapSvg(svg.current, instance.state.rect),
        "videohub-mindmap.svg",
        "image/svg+xml;charset=utf-8",
      );
    } catch {
      if (map.current === instance)
        setError("SVG 导出失败，请重新渲染；仍可导出 Markdown。");
    }
  }
  return (
    <div className="mindmap-viewer">
      <div className="map-toolbar" aria-label="导图工具栏">
        <div>
          <button
            className="icon-button"
            title="放大"
            aria-label="放大"
            disabled={!ready || loading}
            onClick={() => void adjust(1.25)}
          >
            <Icon name="plus" size={17} />
          </button>
          <button
            className="icon-button"
            title="缩小"
            aria-label="缩小"
            disabled={!ready || loading}
            onClick={() => void adjust(0.8)}
          >
            <Icon name="minus" size={17} />
          </button>
          <button
            className="icon-button"
            title="适应画布"
            aria-label="适应画布"
            disabled={!ready || loading}
            onClick={() => void adjust()}
          >
            <Icon name="fit" size={17} />
          </button>
        </div>
        <div className="map-export-group">
          <button
            className="export-button"
            title="下载 Markdown 文件"
            onClick={() =>
              downloadText(
                markdown,
                "videohub-mindmap.md",
                "text/markdown;charset=utf-8",
              )
            }
          >
            <Icon name="text" size={15} />
            <span>Markdown</span>
          </button>
          <button
            className="export-button"
            title="下载 SVG 矢量图"
            disabled={!ready || loading || !!error}
            onClick={() => void exportSvg()}
          >
            <Icon name="download" size={15} />
            <span>SVG</span>
          </button>
        </div>
      </div>
      {loading && <p className="muted map-message">正在构建画布…</p>}
      {error && (
        <p className="error-notice">
          {error}
          <button
            className="text-button"
            onClick={() => setAttempt((value) => value + 1)}
          >
            重新渲染
          </button>
        </p>
      )}
      <svg
        ref={svg}
        className="mindmap-svg videohub-mindmap"
        viewBox="0 0 640 480"
        width="640"
        height="480"
        aria-label="视频内容思维导图"
        role="img"
      />
      <div className="map-hint">滚轮缩放 · 拖动平移 · 点击节点展开</div>
    </div>
  );
}
