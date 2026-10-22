<script setup lang="ts">
import { onMounted, onUnmounted } from "vue";
import AppHeader from "./components/AppHeader.vue";
import AppFooter from "./components/AppFooter.vue";
import TaskHistory from "./components/TaskHistory.vue";
import { useWorkspace } from "./stores/workspace";
const store = useWorkspace();
onMounted(store.restore);
onUnmounted(store.stopAll);
</script>
<template>
  <AppHeader />
  <main id="main"><RouterView :key="$route.fullPath" /></main>
  <AppFooter /><TaskHistory />
</template>
