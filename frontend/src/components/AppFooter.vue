<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { api } from "../api";
import { get } from "../api/client";
import type { LivenessStatus } from "../types/api";
const states = ref({
  API: "checking",
  PostgreSQL: "checking",
  Redis: "checking",
});
let timer: ReturnType<typeof setTimeout>, controller: AbortController;
let disposed = false;
async function refresh() {
  controller = new AbortController();
  try {
    await api.ready(controller.signal);
    if (!disposed)
      states.value = { API: "ready", PostgreSQL: "ready", Redis: "ready" };
  } catch {
    if (!disposed) {
      states.value = {
        API: "unavailable",
        PostgreSQL: "unknown",
        Redis: "unknown",
      };
      try {
        await get<LivenessStatus>("/health/live", controller.signal);
        if (!disposed) states.value.API = "ready";
      } catch {
        /* Preserve unavailable state. */
      }
    }
  }
  if (!disposed) timer = setTimeout(refresh, 15000);
}
onMounted(refresh);
onUnmounted(() => {
  disposed = true;
  clearTimeout(timer);
  controller?.abort();
});
</script>
<template>
  <footer class="app-footer">
    <span
      >VIDEOHUB AI
      <span class="footer-divider">/</span> 让内容成为你的知识。</span
    >
    <div class="health-states" aria-live="polite">
      <span
        v-for="(state, name) in states"
        :key="name"
        :title="
          state === 'ready'
            ? '已就绪'
            : state === 'checking'
              ? '检查中'
              : state === 'unknown'
                ? '依赖状态未知'
                : '无法连接'
        "
        class="health"
        ><i
          :class="{ ready: state === 'ready', down: state === 'unavailable' }"
        ></i
        >{{ name }}<span class="sr-only">{{ state }}</span></span
      >
    </div>
  </footer>
</template>
