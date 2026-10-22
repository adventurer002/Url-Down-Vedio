<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useWorkspace } from "../stores/workspace";
import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import { terminal } from "../composables/useTaskPoller";
import type {
  JobKind,
  SummaryResult,
  TranscriptResult,
  MindMapResult,
} from "../types/api";
import { filesize, timecode } from "../utils";
import Icon from "../components/Icon.vue";
import ErrorNotice from "../components/ErrorNotice.vue";
import TaskCard from "../components/TaskCard.vue";
import MindMapViewer from "../components/MindMapViewer.vue";
const store = useWorkspace(),
  route = useRoute();
const video = computed(() =>
  store.videos.find((v) => v.key === route.params.id),
);
const jobs = computed(() =>
  store.tasks.filter((t) => t.videoKey === route.params.id),
);
const tab = ref("content"),
  raw = ref(false),
  collapsed = ref(false),
  thumbnailFailed = ref(false);
const error = ref<ApiError | null>(null),
  resultError = ref<ApiError | null>(null),
  summary = ref<SummaryResult>(),
  transcript = ref<TranscriptResult>(),
  mindmap = ref<MindMapResult>();
const loaded = new Set<string>(),
  loadingResults = ref(false);
function busy(kind: JobKind) {
  return (
    store.submitting.includes(`${video.value?.key}:${kind}`) ||
    jobs.value.some((t) => t.kind === kind && !terminal(t.event.status))
  );
}
async function create(kind: JobKind, label: string, presetId?: string) {
  if (!video.value) return;
  error.value = null;
  try {
    await store.create(video.value, kind, label, presetId);
  } catch (e) {
    error.value = normalizeError(e);
  }
}
const refreshingMetadata = ref(false);
async function refreshMetadata() {
  if (!video.value || refreshingMetadata.value) return;
  refreshingMetadata.value = true;
  error.value = null;
  try {
    store.remember(video.value.url, await api.parse(video.value.url));
  } catch (e) {
    error.value = normalizeError(e);
  } finally {
    refreshingMetadata.value = false;
  }
}
const latest = (kind: JobKind) => jobs.value.find((t) => t.kind === kind);
let reloadQueued = false;
async function loadResults() {
  if (loadingResults.value) {
    reloadQueued = true;
    return;
  }
  loadingResults.value = true;
  resultError.value = null;
  try {
    // Only the latest completed result per job kind belongs in this workspace.
    const completed = jobs.value.filter((t) => t.event.status === "completed");
    const candidates = completed.filter(
      (t, i) => completed.findIndex((other) => other.kind === t.kind) === i,
    );
    for (const task of candidates.reverse()) {
      if (task.event.status !== "completed" || !task.snapshot?.result_payload)
        continue;
      const payload = task.snapshot.result_payload;
      if (payload.transcript_id && !loaded.has(payload.transcript_id)) {
        transcript.value = await api.transcript(payload.transcript_id);
        loaded.add(payload.transcript_id);
      }
      if (payload.result_id && !loaded.has(payload.result_id)) {
        const result = await api.result(payload.result_id);
        if (result.result_type === "summary") summary.value = result;
        else mindmap.value = result;
        if (!transcript.value || transcript.value.id !== result.transcript_id) {
          transcript.value = await api.transcript(result.transcript_id);
          loaded.add(result.transcript_id);
        }
        loaded.add(payload.result_id);
      }
    }
  } catch (e) {
    resultError.value = normalizeError(e);
  } finally {
    loadingResults.value = false;
    if (reloadQueued) {
      reloadQueued = false;
      void loadResults();
    }
  }
}
watch(
  () =>
    jobs.value
      .map(
        (t) =>
          `${t.id}:${t.snapshot?.result_payload?.result_id || t.snapshot?.result_payload?.transcript_id || ""}`,
      )
      .join("|"),
  loadResults,
  { immediate: true },
);
</script>
<template>
  <div v-if="!video" class="missing-workspace">
    <span class="eyebrow">WORKSPACE NOT FOUND</span>
    <h1>重新连接你的内容。</h1>
    <p>此视频尚未在当前浏览器解析，或本地记录已清除。</p>
    <RouterLink to="/" class="button primary"
      >返回首页，粘贴视频链接 <Icon name="arrow"
    /></RouterLink>
  </div>
  <div v-else class="workspace-page">
    <div class="workspace-breadcrumb">
      <RouterLink to="/">首页</RouterLink><span>/</span><span>视频工作台</span
      ><span class="workspace-tag">WORKSPACE</span>
    </div>
    <div class="workspace-heading">
      <div>
        <span class="eyebrow">MAKE IT YOURS</span>
        <h1>一个视频，无限收获。</h1>
      </div>
      <span class="workspace-heading-note">下载 · 阅读 · 理解</span>
    </div>
    <ErrorNotice :error="error" /><ErrorNotice :error="resultError"
      ><button class="text-button" @click="loadResults">
        重新加载结果
      </button></ErrorNotice
    >
    <nav class="mobile-tabs" aria-label="工作台分区">
      <button
        v-for="item in [
          { id: 'media', label: '视频与下载' },
          { id: 'content', label: '文稿与总结' },
          { id: 'mindmap', label: '思维导图' },
        ]"
        :key="item.id"
        :class="{ active: tab === item.id }"
        :aria-pressed="tab === item.id"
        @click="tab = item.id"
      >
        {{ item.label }}
      </button>
    </nav>
    <div class="workspace-grid" :class="{ 'map-collapsed': collapsed }">
      <aside
        class="workspace-panel media-panel"
        :class="{ 'mobile-active': tab === 'media' }"
      >
        <div class="panel-heading">
          <span class="panel-number">01</span>
          <h2>视频与下载</h2>
          <Icon name="download" :size="18" />
        </div>
        <div class="thumbnail">
          <img
            v-if="video.metadata.thumbnail && !thumbnailFailed"
            :src="video.metadata.thumbnail"
            alt="视频封面"
            referrerpolicy="no-referrer"
            @error="thumbnailFailed = true"
          />
          <div v-else class="thumbnail-placeholder">
            <Icon name="play" :size="34" /><span>VIDEOHUB</span>
          </div>
          <span class="duration">{{ timecode(video.metadata.duration) }}</span>
        </div>
        <div class="video-meta">
          <button
            v-if="video.partial"
            class="button secondary full"
            :disabled="refreshingMetadata"
            @click="refreshMetadata"
          >
            {{ refreshingMetadata ? "正在更新档位…" : "重新解析可用下载档位" }}
          </button>
          <span class="platform-badge">{{ video.metadata.platform }}</span>
          <h2>{{ video.metadata.title }}</h2>
          <p>{{ video.metadata.uploader || "公开资源" }}</p>
        </div>
        <div class="download-section">
          <div class="mini-heading">
            选择下载格式<span
              >{{ video.metadata.download_options.length }} 个选项</span
            >
          </div>
          <button
            v-for="option in video.metadata.download_options"
            :key="option.id"
            class="format-button"
            :disabled="busy('download')"
            @click="create('download', option.label, option.id)"
          >
            <span
              ><strong>{{
                option.height
                  ? option.height + "P " + option.container.toUpperCase()
                  : "仅音频 MP3"
              }}</strong
              ><small>{{ filesize(option.estimated_size_bytes) }}</small></span
            ><Icon name="download" :size="17" />
          </button>
          <p v-if="!video.metadata.download_options.length" class="muted">
            该视频暂无可下载的档位。
          </p>
          <TaskCard v-if="latest('download')" :task="latest('download')!" />
          <div class="audio-section">
            <span class="mini-heading">只想听声音？</span
            ><button
              class="button secondary full"
              :disabled="busy('audio_extraction')"
              @click="create('audio_extraction', '纯音频 MP3')"
            >
              <Icon name="download" :size="16" />提取纯音频 MP3</button
            ><TaskCard
              v-if="latest('audio_extraction')"
              :task="latest('audio_extraction')!"
            />
          </div>
          <p class="panel-footnote">
            仅处理公开免登录资源。<br />请尊重创作者与内容版权。
          </p>
        </div>
      </aside>
      <section
        class="workspace-panel content-panel"
        :class="{ 'mobile-active': tab === 'content' }"
      >
        <div class="panel-heading">
          <span class="panel-number">02</span>
          <h2>文稿与总结</h2>
          <Icon name="text" :size="18" />
        </div>
        <div class="content-actions">
          <button
            class="button secondary"
            :disabled="busy('transcription')"
            @click="create('transcription', '字幕 / 语音转写')"
          >
            {{
              busy("transcription") ? "正在获取…" : "获取字幕 / 转写"
            }}</button
          ><button
            class="button primary"
            :disabled="busy('ai_summary')"
            @click="create('ai_summary', 'AI 结构化总结')"
          >
            <span class="sparkle">✦</span
            >{{ busy("ai_summary") ? "正在总结…" : "生成 AI 总结" }}
          </button>
        </div>
        <div class="content-tasks">
          <TaskCard
            v-if="latest('transcription')"
            :task="latest('transcription')!"
          /><TaskCard
            v-if="latest('ai_summary')"
            :task="latest('ai_summary')!"
          />
        </div>
        <div v-if="summary || transcript" class="reading-content">
          <div class="reading-heading">
            <span class="source-badge">{{
              (summary?.content_json.source_basis ||
                transcript?.source_type) === "subtitle"
                ? "基于平台字幕"
                : "基于语音转写"
            }}</span
            ><span v-if="summary" class="model-label">{{
              summary.llm_provider === "heuristic"
                ? "本地摘录模式"
                : summary.llm_model
            }}</span>
          </div>
          <template v-if="summary"
            ><div class="reading-section">
              <span class="eyebrow">THE BIG PICTURE</span>
              <h3>核心概述</h3>
              <p class="overview-text">{{ summary.content_json.overview }}</p>
            </div>
            <div class="reading-section">
              <h3>值得记住的要点</h3>
              <ol class="key-points">
                <li
                  v-for="(point, i) in summary.content_json.key_points"
                  :key="i"
                >
                  <span>{{ String(i + 1).padStart(2, "0") }}</span>
                  <p>{{ point }}</p>
                </li>
              </ol>
            </div>
            <div class="reading-section">
              <h3>沿着时间，理解内容</h3>
              <article
                v-for="(chapter, i) in summary.content_json.chapters"
                :key="i"
                class="chapter"
              >
                <span class="timestamp"
                  >[{{ timecode(chapter.start_seconds) }}]</span
                >
                <div>
                  <h4>{{ chapter.title }}</h4>
                  <p>{{ chapter.summary }}</p>
                </div>
              </article>
            </div>
            <p class="usage-line">
              {{
                summary.input_tokens == null
                  ? "Token 用量未提供"
                  : `${summary.input_tokens} 输入 / ${summary.output_tokens ?? "—"} 输出 Token`
              }}<span v-if="summary.duration_ms != null">
                · {{ (summary.duration_ms / 1000).toFixed(1) }} 秒</span
              >
            </p></template
          >
          <div v-if="transcript" class="transcript-section">
            <button
              class="transcript-toggle"
              @click="raw = !raw"
              :aria-expanded="raw"
            >
              <span
                >{{ summary ? "查看原始文稿" : "原始字幕文稿" }}
                <small
                  >{{ transcript.language.toUpperCase() }} ·
                  {{ transcript.segments.length }} 段</small
                ></span
              ><Icon :name="raw ? 'minus' : 'plus'" :size="18" />
            </button>
            <div v-if="raw || !summary" class="transcript-segments">
              <div v-for="(segment, i) in transcript.segments" :key="i">
                <time>{{ timecode(segment.start) }}</time>
                <p>{{ segment.text }}</p>
              </div>
            </div>
          </div>
        </div>
        <div v-else class="empty-state content-empty">
          <span class="empty-icon"><Icon name="text" :size="30" /></span>
          <h3>精彩内容，值得细读。</h3>
          <p>获取字幕，或让 AI 为你提炼要点。<br />长视频，也能一目了然。</p>
          <div class="empty-preview">
            <div></div>
            <div></div>
            <div></div>
          </div>
        </div>
      </section>
      <section
        class="workspace-panel map-panel"
        :class="{ 'mobile-active': tab === 'mindmap' }"
      >
        <div class="panel-heading">
          <span class="panel-number">03</span>
          <h2>思维导图</h2>
          <button
            class="icon-button collapse-map"
            :aria-label="collapsed ? '展开思维导图' : '折叠思维导图'"
            @click="collapsed = !collapsed"
          >
            <Icon :name="collapsed ? 'plus' : 'minus'" :size="17" />
          </button>
        </div>
        <div class="map-content" v-show="!collapsed">
          <div class="map-intro">
            <p>把内容连成一张知识地图。</p>
            <button
              class="button primary full"
              :disabled="busy('ai_mindmap')"
              @click="create('ai_mindmap', 'AI 思维导图')"
            >
              <Icon name="map" :size="16" />{{
                busy("ai_mindmap")
                  ? "正在构建…"
                  : mindmap
                    ? "重新生成思维导图"
                    : "生成思维导图"
              }}
            </button>
          </div>
          <TaskCard
            v-if="latest('ai_mindmap')"
            :task="latest('ai_mindmap')!"
          /><MindMapViewer v-if="mindmap" :result="mindmap" />
          <div v-else class="empty-state map-empty">
            <div class="map-preview" aria-hidden="true">
              <div class="map-root">核心主题</div>
              <div class="map-branches">
                <span>观点一</span><span>观点二</span><span>观点三</span>
              </div>
            </div>
            <h3>看见知识的形状。</h3>
            <p>从主题到细节，<br />在可交互的导图中探索关联。</p>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>
