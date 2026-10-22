<script setup lang="ts">
import { ref, watch, nextTick, onUnmounted } from "vue";
import { useWorkspace } from "../stores/workspace";
import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import type { CleanupResult } from "../types/api";
import Icon from "./Icon.vue";
import TaskCard from "./TaskCard.vue";
import ErrorNotice from "./ErrorNotice.vue";
const store = useWorkspace(),
  dialog = ref<HTMLDialogElement>();
const error = ref<ApiError | null>(null),
  cleaning = ref(false),
  cleanup = ref<CleanupResult>();
let previousFocus: HTMLElement | null = null,
  refreshTimer: ReturnType<typeof setTimeout> | undefined;
async function refresh(reset = true) {
  error.value = null;
  try {
    await store.loadHistory(reset);
  } catch (e) {
    error.value = normalizeError(e);
  }
}
async function sync(force = false) {
  if (force || !store.historyLoaded || store.historyOffset <= 20) await refresh();
  if (store.historyOpen) refreshTimer = setTimeout(sync, 15000);
}
watch(
  () => store.historyOpen,
  async (open) => {
    await nextTick();
    if (open) {
      previousFocus = document.activeElement as HTMLElement;
      dialog.value?.showModal();
      void sync(true);
    } else {
      clearTimeout(refreshTimer);
      dialog.value?.close();
      previousFocus?.focus();
    }
  },
);
onUnmounted(() => clearTimeout(refreshTimer));
function close() {
  store.historyOpen = false;
}
async function clean() {
  if (cleaning.value) return;
  cleaning.value = true;
  error.value = null;
  cleanup.value = undefined;
  try {
    cleanup.value = await api.cleanup();
    await refresh();
  } catch (e) {
    error.value = normalizeError(e);
  } finally {
    cleaning.value = false;
  }
}
function bytes(value: number) {
  return value < 1024
    ? `${value} B`
    : value < 1024 ** 2
      ? `${(value / 1024).toFixed(1)} KB`
      : `${(value / 1024 ** 2).toFixed(2)} MB`;
}
</script>
<template>
  <dialog
    ref="dialog"
    class="history-dialog"
    aria-labelledby="history-title"
    @cancel.prevent="close"
    @click="
      (event) => {
        if (event.target === dialog) close();
      }
    "
  >
    <div class="drawer">
      <div class="drawer-heading">
        <div>
          <span class="eyebrow">YOUR ACTIVITY</span>
          <h2 id="history-title">任务历史</h2>
        </div>
        <button class="icon-button" aria-label="关闭任务历史" @click="close">
          <Icon name="close" />
        </button>
      </div>
      <div class="history-summary">
        <p class="muted history-hint">
          来自本地服务器 · {{ store.historyTotal }} 个任务
        </p>
        <button
          class="text-button"
          :disabled="store.historyLoading"
          @click="refresh()"
        >
          刷新列表
        </button>
      </div>
      <ErrorNotice :error="error" />
      <p
        v-if="store.historyLoading && !store.historyLoaded"
        class="muted"
        role="status"
      >
        正在读取服务器任务…
      </p>
      <div
        v-if="!store.historyTasks.length && !store.historyLoading"
        class="empty-state"
      >
        <Icon name="history" :size="36" />
        <h3>一切从一个链接开始</h3>
        <p>下载、转写与 AI 任务会记录在这里。</p>
      </div>
      <article
        v-for="task in store.historyTasks"
        :key="task.id"
        class="history-item"
      >
        <RouterLink
          v-if="task.videoAvailable !== false"
          :to="'/video/' + encodeURIComponent(task.videoKey)"
          @click="close"
          >{{ task.title }}</RouterLink
        ><strong v-else>{{ task.title }}</strong>
        <time class="history-date">{{
          new Date(task.createdAt).toLocaleString("zh-CN")
        }}</time>
        <TaskCard :task="task" />
        <RouterLink
          v-if="
            task.videoAvailable !== false &&
            task.event.status === 'completed' &&
            (task.snapshot?.result_payload?.result_id ||
              task.snapshot?.result_payload?.transcript_id)
          "
          class="history-result-link"
          :to="'/video/' + encodeURIComponent(task.videoKey)"
          @click="close"
          >查看文稿 / AI 结果 ↗</RouterLink
        >
      </article>
      <button
        v-if="store.historyOffset < store.historyTotal"
        class="button secondary full"
        :disabled="store.historyLoading"
        @click="refresh(false)"
      >
        {{ store.historyLoading ? "正在读取…" : "加载更多任务" }}
      </button>
      <div class="cleanup-section">
        <span class="eyebrow">KEEP YOUR SPACE CLEAR</span>
        <p>
          仅清理已过期文件与安全可移除的临时文件，保留当前有效结果和运行中的任务。
        </p>
        <button
          class="button secondary full"
          :disabled="cleaning"
          @click="clean"
        >
          {{ cleaning ? "正在释放空间…" : "清理过期缓存文件" }}
        </button>
        <p v-if="cleanup" class="cleanup-result" role="status">
          已释放 {{ bytes(cleanup.freed_bytes) }} ·
          {{ cleanup.files + cleanup.orphan_files }} 个文件 /
          {{ cleanup.temporary_directories }} 个临时目录
        </p>
      </div>
    </div>
  </dialog>
</template>
