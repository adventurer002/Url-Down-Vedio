<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import { useWorkspace } from "../stores/workspace";
import ErrorNotice from "../components/ErrorNotice.vue";
import Icon from "../components/Icon.vue";
const url = ref(""),
  loading = ref(false),
  error = ref<ApiError | null>(null);
const router = useRouter(),
  store = useWorkspace();
async function parse() {
  if (loading.value) return;
  error.value = null;
  const input = url.value.trim();
  try {
    const parsed = new URL(input);
    if (!["http:", "https:"].includes(parsed.protocol)) throw new Error();
  } catch {
    error.value = new ApiError(
      "INVALID_URL",
      "请输入完整的 http 或 https 视频链接。",
    );
    return;
  }
  loading.value = true;
  try {
    const metadata = await api.parse(input);
    const key = store.remember(input, metadata);
    await router.push("/video/" + encodeURIComponent(key));
  } catch (e) {
    error.value = normalizeError(e);
  } finally {
    loading.value = false;
  }
}
const features = [
  {
    n: "01",
    icon: "download",
    title: "视频下载",
    description: "选择合适的清晰度，让精彩随时可看。",
    note: "原生档位 · 本地保存",
  },
  {
    n: "02",
    icon: "text",
    title: "音频与文稿",
    description: "留下声音与文字，换一种方式吸收内容。",
    note: "纯音频提取 · 字幕优先",
  },
  {
    n: "03",
    icon: "map",
    title: "AI 总结与导图",
    description: "从长视频中提炼重点，让知识脉络清晰可见。",
    note: "章节摘要 · 交互导图",
  },
];
</script>
<template>
  <div class="home-page">
    <section class="hero">
      <div class="hero-kicker">
        <span class="tiny-square"></span> YOUR VIDEO. YOUR KNOWLEDGE.
      </div>
      <h1>下载视频，<br class="mobile-break" /><span>理解内容。</span></h1>
      <p class="hero-description">
        一个链接，连接视频、文稿与灵感。<br
          class="mobile-break"
        />让每一次观看，都有所收获。
      </p>
      <form class="parse-form" @submit.prevent="parse" :aria-busy="loading">
        <label class="sr-only" for="video-url">视频链接</label>
        <div class="url-field">
          <Icon name="link" :size="22" /><input
            id="video-url"
            v-model="url"
            type="url"
            required
            maxlength="8192"
            placeholder="粘贴公开的视频链接…"
            :disabled="loading"
            autocomplete="url"
            :aria-invalid="!!error"
          /><button class="button primary" :disabled="loading" type="submit">
            <span v-if="loading" class="spinner"></span
            >{{ loading ? "正在解析" : "开始解析"
            }}<Icon v-if="!loading" name="arrow" :size="18" />
          </button>
        </div>
        <ErrorNotice :error="error"
          ><button
            class="text-button"
            type="button"
            :disabled="loading"
            @click="parse"
          >
            保留链接，重新尝试 ↗
          </button></ErrorNotice
        >
      </form>
      <div class="hero-footnote">
        <span class="mini-dot"></span>仅支持公开免登录资源<span
          class="footnote-separator"
          >/</span
        >所有操作，尽在一个工作台
      </div>
    </section>
    <section class="feature-section" aria-label="功能介绍">
      <div class="section-caption">
        <span>更少操作，更多收获</span><span>THREE WAYS TO GO FURTHER</span>
      </div>
      <div class="feature-grid">
        <article
          v-for="feature in features"
          :key="feature.n"
          class="feature-card"
        >
          <div class="feature-top">
            <Icon :name="feature.icon" :size="26" /><span>{{ feature.n }}</span>
          </div>
          <h2>{{ feature.title }}</h2>
          <p>{{ feature.description }}</p>
          <div class="feature-note">
            {{ feature.note }}<Icon name="arrow" :size="16" />
          </div>
        </article>
      </div>
    </section>
    <div class="home-bottom">
      <span>为专注而设计。</span><span>从第一帧，到最后一个想法。</span>
    </div>
  </div>
</template>
