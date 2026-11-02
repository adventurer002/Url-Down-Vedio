import { Markmap } from "markmap-view";
import {
  createBranchTheme,
  mapLineWidth,
  mindmapCss,
  orthogonalPath,
  type MapLink,
  type MapNode,
} from "./mindmapTheme";

/** Keep Markmap layout/gestures, adapt its public render lifecycle locally. */
class OrthogonalMarkmap extends Markmap {
  private disposed = false;
  private readonly theme = createBranchTheme();
  private readonly reportError: () => void;
  constructor(svg: SVGSVGElement, onError: () => void) {
    super(svg, {
      duration: 0,
      autoFit: false,
      maxWidth: 250,
      maxInitialScale: 1,
      paddingX: 12,
      spacingHorizontal: 64,
      spacingVertical: 16,
      initialExpandLevel: 3,
      lineWidth: mapLineWidth,
      style: () => mindmapCss,
    });
    this.reportError = onError;
    this.options.color = (node: MapNode) => this.theme(node).color;
    this.g.attr("class", "videohub-mindmap-canvas");
    this.handleClick = (event, node) => {
      this.toggleFromEvent(event, node);
    };
  }

  private toggleFromEvent(
    event: Pick<KeyboardEvent, "metaKey" | "ctrlKey">,
    node: MapNode,
  ) {
    const recursive =
      this.options.toggleRecursively !==
      Boolean(event.metaKey || event.ctrlKey);
    void this.toggleNode(node, recursive).catch(this.reportError);
  }

  override async renderData(origin?: MapNode) {
    if (this.disposed) return;
    await super.renderData(origin);
    if (this.disposed || !this.state.data) return;
    const visible = new Set<number>();
    const visit = (node: MapNode) => {
      visible.add(node.state.id);
      if (!node.payload?.fold) node.children.forEach(visit);
    };
    visit(this.state.data);
    const links = this.g.selectAll<SVGPathElement, MapLink>(
      "path.markmap-link",
    );
    // Cancel Markmap's pending curve writes. Exiting paths are removed explicitly.
    links.interrupt();
    links
      .filter((edge: MapLink) => !visible.has(edge.target.state.id))
      .remove();
    links
      .filter((edge: MapLink) => visible.has(edge.target.state.id))
      .attr("d", (edge: MapLink) => orthogonalPath(edge, this.options.paddingX))
      .attr("stroke", (edge: MapLink) => this.theme(edge.target).color)
      .attr("stroke-width", (edge: MapLink) => mapLineWidth(edge.target));
    const theme = this.theme;
    const nodes = this.g.selectAll<SVGGElement, MapNode>("g.markmap-node");
    nodes.interrupt();
    nodes.filter((node: MapNode) => !visible.has(node.state.id)).remove();
    const active = nodes.filter((node: MapNode) => visible.has(node.state.id));
    // Finish zero-duration node writes synchronously with their adapted links.
    active
      .attr(
        "transform",
        (node: MapNode) =>
          `translate(${node.state.rect.x},${node.state.rect.y})`,
      )
      .each(function (this: SVGGElement, node: MapNode) {
        const colors = theme(node);
        this.style.setProperty("--mm-color", colors.color);
        this.style.setProperty("--mm-background", colors.background);
        this.style.setProperty("--mm-border", colors.border);
      });
    active
      .selectAll<SVGForeignObjectElement, MapNode>("foreignObject")
      .interrupt()
      .style("opacity", 1);
    active
      .selectAll<SVGCircleElement, MapNode>("circle")
      .interrupt()
      .attr(
        "cx",
        (node: MapNode) => node.state.rect.width - this.options.paddingX + 8,
      )
      .attr("cy", (node: MapNode) => node.state.rect.height / 2)
      .attr("r", 5)
      .attr("stroke-width", 1.5)
      .attr("stroke", (node: MapNode) => this.theme(node).color)
      .attr("fill", (node: MapNode) =>
        node.payload?.fold ? this.theme(node).color : "#ffffff",
      )
      .attr("role", "button")
      .attr("tabindex", 0)
      .attr("aria-expanded", (node: MapNode) => String(!node.payload?.fold))
      .attr("aria-label", function (this: SVGCircleElement, node: MapNode) {
        const title =
          this.parentElement?.querySelector("foreignObject")?.textContent ||
          "节点";
        return `${node.payload?.fold ? "展开" : "收起"}${title}`;
      })
      .on("keydown.videohub", (event: KeyboardEvent, node: MapNode) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        event.stopPropagation();
        this.toggleFromEvent(event, node);
      });
  }

  override destroy() {
    if (this.disposed) return;
    this.disposed = true;
    this.g.interrupt();
    this.g.selectAll("*").interrupt();
    this.svg.interrupt().on("wheel", null);
    this.svg.classed(this.state.id, false).classed("markmap", false);
    super.destroy();
  }
}

export function createMindmap(svg: SVGSVGElement, onError: () => void) {
  return new OrthogonalMarkmap(svg, onError);
}
