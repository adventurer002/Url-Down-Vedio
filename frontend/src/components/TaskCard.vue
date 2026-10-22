<script setup lang="ts">
import { ref } from "vue";
import type { LocalTask } from "../stores/workspace";
import { useWorkspace } from "../stores/workspace";
import { terminal } from "../composables/useTaskPoller";
import { fileDownloadUrl, normalizeError } from "../api/client";
import Icon from "./Icon.vue";
const props = defineProps<{ task: LocalTask }>();
const store = useWorkspace(),
  busy = ref(false),
  error = ref("");
const labels = {
  queued: "等待执行",
  running: "执行中",
  processing: "处理中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  expired: "已过期",
};
const stages: Record<string, string> = {
  queued: "等待 Worker",
  download: "下载媒体",
  downloading: "下载媒体",
  processing: "处理媒体",
  subtitles: "获取平台字幕",
  audio: "准备纯音频",
  asr: "语音转写",
  summary: "提炼章节要点",
  mindmap: "构建思维导图",
  ai: "整理内容",
  running: "正在执行",
  completed: "处理完成",
};
async function cancel() {
  busy.value = true;
  error.value = "";
  try {
    await store.cancel(props.task);
  } catch (e) {
    error.value = normalizeError(e).message;
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <div class="task-card">
    <div class="task-line">
      <span>{{ task.label }}</span
      ><span class="task-status" :data-status="task.event.status"
        >{{ labels[task.event.status] }} <b>{{ task.event.progress }}%</b></span
      >
    </div>
    <div
      class="progress-track"
      role="progressbar"
      :aria-valuenow="task.event.progress"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-label="task.label"
    >
      <div :style="{ width: task.event.progress + '%' }"></div>
    </div>
    <div class="task-bottom">
      <small
        >{{ stages[task.event.stage] || labels[task.event.status]
        }}<span v-if="task.snapshot?.result_payload?.cache_hit">
          · 来自缓存</span
        ></small
      ><button
        v-if="!terminal(task.event.status)"
        class="text-button"
        :disabled="busy || task.snapshot?.cancel_requested"
        @click="cancel"
      >
        {{
          busy || task.snapshot?.cancel_requested ? "正在取消…" : "取消任务"
        }}</button
      ><a
        v-if="
          task.event.status === 'completed' &&
          task.snapshot?.result_payload?.file_id
        "
        class="save-link"
        :href="fileDownloadUrl(task.snapshot.result_payload.file_id)"
        ><Icon name="download" :size="14" />保存到本地</a
      >
    </div>
    <p v-if="task.event.error_code" class="task-error">
      {{ task.snapshot?.error_message || labels[task.event.status] }} ·
      {{ task.event.error_code }}
    </p>
    <p v-if="task.trackingError || error" class="task-error">
      {{ error || task.trackingError
      }}<span v-if="task.trackingError"> · 正在重连</span>
    </p>
  </div>
</template>
