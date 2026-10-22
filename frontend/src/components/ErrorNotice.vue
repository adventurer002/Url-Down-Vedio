<script setup lang="ts">
import type { ApiError } from "../api/client";
defineProps<{ error: ApiError | null }>();
</script>
<template>
  <div v-if="error" class="error-notice" role="alert">
    <strong>{{ error.message }}</strong
    ><code>{{ error.code }}</code
    ><small v-if="error.request_id">请求编号：{{ error.request_id }}</small
    ><slot />
  </div>
</template>
