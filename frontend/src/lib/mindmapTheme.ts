import type { Markmap } from "markmap-view";

export type MapNode = Parameters<Markmap["options"]["color"]>[0];
export interface MapLink {
  source: MapNode;
  target: MapNode;
}
interface BranchTheme {
  color: string;
  background: string;
  border: string;
}

const rootTheme: BranchTheme = {
  color: "#1f2937",
  background: "#1f2937",
  border: "#1f2937",
};
const branches: BranchTheme[] = [
  { color: "#2563eb", background: "#eff6ff", border: "#bfdbfe" },
  { color: "#047857", background: "#ecfdf5", border: "#a7f3d0" },
  { color: "#c2410c", background: "#fff7ed", border: "#fed7aa" },
  { color: "#7c3aed", background: "#f5f3ff", border: "#ddd6fe" },
  { color: "#0e7490", background: "#ecfeff", border: "#a5f3fc" },
  { color: "#be185d", background: "#fdf2f8", border: "#fbcfe8" },
];

/** Assign hues by first-level branch, never by visible order after folding. */
export function createBranchTheme() {
  const assigned = new Map<string, BranchTheme>();
  return (node: MapNode): BranchTheme => {
    if (node.state.depth <= 1) return rootTheme;
    const branch = node.state.path.split(".").slice(0, 2).join(".");
    if (!assigned.has(branch))
      assigned.set(branch, branches[assigned.size % branches.length]);
    return assigned.get(branch)!;
  };
}

export function mapLineWidth(node: MapNode) {
  return node.state.depth <= 2 ? 2.4 : node.state.depth === 3 ? 1.7 : 1.2;
}

/** Connect card edges through the shared gutter, using only M/H/V commands. */
export function orthogonalPath({ source, target }: MapLink, paddingX: number) {
  const a = source.state.rect,
    b = target.state.rect;
  const sx = a.x + a.width - paddingX,
    sy = a.y + a.height / 2;
  const tx = b.x + paddingX,
    ty = b.y + b.height / 2;
  const middle = (sx + tx) / 2;
  return `M${sx},${sy}H${middle}V${ty}H${tx}`;
}

// Embedded inside each SVG so exports keep typography, hierarchy and colors.
export const mindmapCss = `
.videohub-mindmap.markmap { background:#fff; color:#293347; font:400 14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
.videohub-mindmap .markmap-link { fill:none; stroke-linejoin:miter; stroke-linecap:butt; }
.videohub-mindmap .markmap-node > line { visibility:hidden; }
.videohub-mindmap .markmap-foreign > div > div { box-sizing:border-box; padding:8px 12px; border:1px solid var(--mm-border,#dbe2ea); border-radius:7px; background:#fff; color:#293347; font-size:13px; font-weight:400; line-height:1.65; overflow-wrap:anywhere; }
.videohub-mindmap .markmap-foreign p { margin:0; font:inherit; color:inherit; }
.videohub-mindmap .markmap-node[data-depth="1"] .markmap-foreign > div > div { padding:12px 16px; background:#1f2937; border-color:#1f2937; color:#fff; border-radius:11px; font-size:20px; font-weight:650; }
.videohub-mindmap .markmap-node[data-depth="2"] .markmap-foreign > div > div { background:var(--mm-background,#f1f5f9); border-color:var(--mm-border,#dbe2ea); font-size:16px; font-weight:600; }
.videohub-mindmap .markmap-node[data-depth="3"] .markmap-foreign > div > div { font-size:14px; }
.videohub-mindmap .markmap-node > circle { cursor:pointer; }
.videohub-mindmap .markmap-node > circle:focus-visible { outline:2px solid var(--mm-color,#2563eb); outline-offset:4px; }
.videohub-mindmap .markmap-foreign a { color:inherit; text-decoration:underline; }
`;

export function serializeMindmapSvg(
  svg: SVGSVGElement,
  bounds: Markmap["state"]["rect"],
) {
  const values = [bounds.x1, bounds.y1, bounds.x2, bounds.y2];
  if (
    !values.every(Number.isFinite) ||
    bounds.x2 < bounds.x1 ||
    bounds.y2 < bounds.y1
  )
    throw new Error("Invalid map bounds");
  const padding = 32;
  const x = bounds.x1 - padding,
    y = bounds.y1 - padding;
  const width = Math.max(1, Math.ceil(bounds.x2 - bounds.x1 + padding * 2));
  const height = Math.max(1, Math.ceil(bounds.y2 - bounds.y1 + padding * 2));
  const clone = svg.cloneNode(true) as SVGSVGElement;
  // D3 may set plain `xmlns` attributes on XHTML labels. Normalize their
  // namespaces so XMLSerializer cannot emit duplicate xmlns attributes.
  for (const node of [clone, ...clone.querySelectorAll("[xmlns]")]) {
    node.removeAttribute("xmlns");
    node.setAttributeNS(
      "http://www.w3.org/2000/xmlns/",
      "xmlns",
      node.namespaceURI || "http://www.w3.org/2000/svg",
    );
  }
  clone.setAttribute("viewBox", `${x} ${y} ${width} ${height}`);
  clone.setAttribute("width", String(width));
  clone.setAttribute("height", String(height));
  clone.querySelector(".videohub-mindmap-canvas")?.removeAttribute("transform");
  const background = document.createElementNS(
    "http://www.w3.org/2000/svg",
    "rect",
  );
  background.setAttribute("x", String(x));
  background.setAttribute("y", String(y));
  background.setAttribute("width", String(width));
  background.setAttribute("height", String(height));
  background.setAttribute("fill", "#ffffff");
  clone.prepend(background);
  return new XMLSerializer().serializeToString(clone);
}
