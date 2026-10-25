import { useEffect, useRef, useState } from "react";
import type { Markmap } from "markmap-view";
import type { MindMapResult } from "../types/api";
import { treeToMarkdown, downloadText } from "../utils";
import Icon from "./Icon";
export default function MindMapViewer({ result }: { result: MindMapResult }) {
  const svg = useRef<SVGSVGElement>(null),
    map = useRef<Markmap | undefined>(undefined);
  const [error, setError] = useState(""),
    [loading, setLoading] = useState(false),
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
      rendering = false;
    async function render() {
      if (disposed || rendering || !sizeCanvas()) return;
      rendering = true;
      setLoading(true);
      setError("");
      try {
        const [{ Transformer }, { Markmap }] = await Promise.all([
          import("markmap-lib"),
          import("markmap-view"),
        ]);
        if (disposed || !svg.current || !sizeCanvas()) return;
        map.current?.destroy();
        const transformer = new Transformer();
        transformer.md.set({ html: false });
        const { root } = transformer.transform(markdown);
        map.current = Markmap.create(
          svg.current,
          {
            color: () => "#222222",
            duration: 0,
            autoFit: false,
            maxWidth: 190,
            paddingX: 14,
            initialExpandLevel: 3,
          },
          root,
        );
        await map.current.fit();
      } catch {
        if (!disposed)
          setError("导图暂时无法渲染，请重试；仍可导出 Markdown。");
      } finally {
        rendering = false;
        if (!disposed) setLoading(false);
      }
    }
    const observer = new ResizeObserver(() => {
      if (disposed || !sizeCanvas()) return;
      if (map.current)
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
  function exportSvg() {
    if (!svg.current || !map.current) return;
    const clone = svg.current.cloneNode(true) as SVGSVGElement,
      box = svg.current.getBoundingClientRect();
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    clone.setAttribute("width", String(Math.max(600, box.width)));
    clone.setAttribute("height", String(Math.max(500, box.height)));
    const style = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "style",
    );
    style.textContent =
      ".markmap{font:14px sans-serif;color:#222}.markmap-foreign{line-height:1.5}.markmap-link{fill:none;stroke:#444}.markmap-node circle{fill:#fff;stroke:#222}.markmap-foreign p{margin:0} a{color:inherit}";
    clone.prepend(style);
    downloadText(
      new XMLSerializer().serializeToString(clone),
      "videohub-mindmap.svg",
      "image/svg+xml;charset=utf-8",
    );
  }
  return (
    <div className="mindmap-viewer">
      <div className="map-toolbar" aria-label="导图工具栏">
        <div>
          <button
            className="icon-button"
            title="放大"
            aria-label="放大"
            onClick={() => void adjust(1.25)}
          >
            <Icon name="plus" size={17} />
          </button>
          <button
            className="icon-button"
            title="缩小"
            aria-label="缩小"
            onClick={() => void adjust(0.8)}
          >
            <Icon name="minus" size={17} />
          </button>
          <button
            className="icon-button"
            title="适应画布"
            aria-label="适应画布"
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
            disabled={loading || !!error}
            onClick={exportSvg}
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
        className="mindmap-svg"
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
