<script setup lang="ts">
import Icon from "./Icon.vue";
import { useWorkspace } from "../stores/workspace";
const store = useWorkspace();
</script>
<template>
  <a href="#main" class="skip-link">跳转至主要内容</a>
  <header class="app-header">
    <RouterLink to="/" class="brand" aria-label="VideoHub 首页"
      ><span class="brand-icon"><Icon name="play" :size="20" /></span
      >VIDEOHUB<span class="brand-ai">AI</span></RouterLink
    >
    <div class="header-right">
      <span class="header-note">从视频，到洞见。</span
      ><button class="history-button" @click="store.historyOpen = true">
        <Icon name="history" :size="18" />任务历史<span
          v-if="store.activeCount"
          class="count"
          >{{ store.activeCount }}</span
        >
      </button>
    </div>
  </header>
</template>
