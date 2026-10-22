<script setup lang="ts">
import { nextTick, onUnmounted, ref, watch } from "vue";
import type { Markmap } from "markmap-view";
import type { MindMapResult } from "../types/api";
import { treeToMarkdown, downloadText } from "../utils";
import Icon from "./Icon.vue";
const props = defineProps<{ result: MindMapResult }>();
const svg = ref<SVGSVGElement>(),
  error = ref(""),
  loading = ref(false);
let map: Markmap | undefined,
  generation = 0,
  disposed = false;
const markdown = () =>
  props.result.markdown_export || treeToMarkdown(props.result.content_json);
// D3 cannot resolve SVG percentage lengths while a mobile tab/collapsed pane is hidden.
// Keep a numeric viewBox and only fit/render after the canvas has a real visible size.
function sizeCanvas() {
  if (!svg.value) return false;
  const box = svg.value.getBoundingClientRect();
  if (box.width <= 0 || box.height <= 0) return false;
  svg.value.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
  svg.value.setAttribute("width", String(box.width));
  svg.value.setAttribute("height", String(box.height));
  return true;
}
async function fit() {
  if (!sizeCanvas() || !map || disposed) return;
  try {
    await map.fit();
  } catch {
    if (!disposed) error.value = "画布调整失败，请重新渲染。";
  }
}
async function zoom(scale: number) {
  if (!sizeCanvas() || !map || disposed) return;
  try {
    await map.rescale(scale);
  } catch {
    if (!disposed) error.value = "缩放失败，请重新渲染。";
  }
}
async function render() {
  const run = ++generation;
  loading.value = true;
  error.value = "";
  try {
    const [{ Transformer }, { Markmap }] = await Promise.all([
      import("markmap-lib"),
      import("markmap-view"),
    ]);
    await nextTick();
    if (disposed || run !== generation || !svg.value || !sizeCanvas()) return;
    map?.destroy();
    const transformer = new Transformer();
    // Plain text source is escaped before Markdown parsing; disable raw HTML as a second boundary.
    transformer.md.set({ html: false });
    const { root } = transformer.transform(markdown());
    map = Markmap.create(
      svg.value,
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
    await map.fit();
  } catch {
    if (!disposed)
      error.value = "导图暂时无法渲染，请重试；仍可导出 Markdown。";
  } finally {
    if (run === generation) loading.value = false;
  }
}
watch(() => props.result.id, render, { immediate: true });
const observer = ref<ResizeObserver>();
watch(svg, (element) => {
  observer.value?.disconnect();
  if (element) {
    observer.value = new ResizeObserver(() => {
      if (!sizeCanvas()) return;
      if (map) void fit();
      else if (!loading.value) void render();
    });
    observer.value.observe(element);
  }
});
onUnmounted(() => {
  disposed = true;
  generation++;
  observer.value?.disconnect();
  map?.destroy();
});
function exportSvg() {
  if (!svg.value) return;
  const clone = svg.value.cloneNode(true) as SVGSVGElement;
  const box = svg.value.getBoundingClientRect();
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("width", String(Math.max(600, box.width)));
  clone.setAttribute("height", String(Math.max(500, box.height)));
  // Include the live stylesheet so exported vectors remain readable outside this page.
  const style = document.createElementNS("http://www.w3.org/2000/svg", "style");
  style.textContent =
    ".markmap{font:14px sans-serif;color:#222}.markmap-foreign{line-height:1.5}.markmap-link{fill:none;stroke:#444}.markmap-node circle{fill:#fff;stroke:#222}.markmap-foreign p{margin:0} a{color:inherit}";
  clone.prepend(style);
  downloadText(
    new XMLSerializer().serializeToString(clone),
    "videohub-mindmap.svg",
    "image/svg+xml;charset=utf-8",
  );
}
</script>
<template>
  <div class="mindmap-viewer">
    <div class="map-toolbar" aria-label="导图工具栏">
      <div>
        <button
          class="icon-button"
          title="放大"
          aria-label="放大"
          @click="zoom(1.25)"
        >
          <Icon name="plus" :size="17" /></button
        ><button
          class="icon-button"
          title="缩小"
          aria-label="缩小"
          @click="zoom(0.8)"
        >
          <Icon name="minus" :size="17" /></button
        ><button
          class="icon-button"
          title="适应画布"
          aria-label="适应画布"
          @click="fit"
        >
          <Icon name="fit" :size="17" />
        </button>
      </div>
      <div>
        <button
          class="small-button"
          @click="
            downloadText(
              markdown(),
              'videohub-mindmap.md',
              'text/markdown;charset=utf-8',
            )
          "
        >
          .md ↓</button
        ><button
          class="small-button"
          :disabled="loading || !!error"
          @click="exportSvg"
        >
          .svg ↓
        </button>
      </div>
    </div>
    <p v-if="loading" class="muted map-message">正在构建画布…</p>
    <p v-if="error" class="error-notice">
      {{ error }}<button class="text-button" @click="render">重新渲染</button>
    </p>
    <svg
      ref="svg"
      class="mindmap-svg"
      viewBox="0 0 640 480"
      width="640"
      height="480"
      aria-label="视频内容思维导图"
      role="img"
    ></svg>
    <div class="map-hint">滚轮缩放 · 拖动平移 · 点击节点展开</div>
  </div>
</template>
