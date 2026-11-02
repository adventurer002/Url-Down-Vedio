interface Layer {
  element: HTMLDivElement;
  users: number;
  observer?: MutationObserver;
}
const layers = new Map<HTMLElement, Layer>();

/** Shared stack per document/modal. Popover promotes it beyond stacking contexts. */
export function acquireNoticeLayer(owner: HTMLElement) {
  let layer = layers.get(owner);
  if (!layer) {
    const element = document.createElement("div");
    element.className = "notice-layer";
    element.setAttribute("aria-label", "操作提示");
    if (typeof element.showPopover === "function")
      element.setAttribute("popover", "manual");
    owner.appendChild(element);
    const reveal = () => {
      if (owner instanceof HTMLDialogElement && !owner.open) return;
      if (!element.hasAttribute("popover")) return;
      try {
        element.showPopover();
      } catch {
        element.removeAttribute("popover"); // Fixed positioning is the compatibility fallback.
      }
    };
    const observer =
      owner instanceof HTMLDialogElement
        ? new MutationObserver(reveal)
        : undefined;
    observer?.observe(owner, { attributes: true, attributeFilter: ["open"] });
    reveal();
    layer = { element, users: 0, observer };
    layers.set(owner, layer);
  }
  layer.users++;
  const current = layer;
  return {
    element: current.element,
    release() {
      current.users--;
      if (current.users) return;
      current.observer?.disconnect();
      current.element.remove();
      layers.delete(owner);
    },
  };
}
