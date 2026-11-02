import { ApiError, normalizeError } from "../api/client";
import type { ClipExport, ClipPlan, ClipStrategy } from "../types/clips";
import type { LocalTask, SavedVideo } from "../stores/workspace";

export const clipsCreationEnabled = () =>
  import.meta.env.VITE_CLIPS_ENABLED === "true";
export const isClipTask = (kind: string) =>
  ["ai_clip_plan", "clips_export"].includes(kind);
// Request chronology belongs to tasks; a cached artifact may predate its request.
export type ClipAnalysisOrder = Pick<
  LocalTask,
  "id" | "createdAt" | "snapshot"
>;
export function compareClipAnalyses(
  a: ClipAnalysisOrder,
  b: ClipAnalysisOrder,
) {
  return (
    Date.parse(a.snapshot?.created_at || a.createdAt) -
      Date.parse(b.snapshot?.created_at || b.createdAt) ||
    a.id.localeCompare(b.id)
  );
}
export function latestClipAnalysis(tasks: LocalTask[]) {
  return tasks
    .filter((task) => task.kind === "ai_clip_plan")
    .sort((a, b) => compareClipAnalyses(b, a))[0];
}
export function defaultClipStrategy(_duration: number | null): ClipStrategy {
  return {
    count: 3,
    duration_mode: "auto",
    min_seconds: null,
    max_seconds: null,
    focus: "",
  };
}
export function clipStrategyValid(value: ClipStrategy) {
  const common =
    Number.isInteger(value.count) &&
    value.count >= 1 &&
    value.count <= 5 &&
    typeof value.focus === "string" &&
    value.focus.length <= 200;
  if (!common) return false;
  if (value.duration_mode === "auto")
    return value.min_seconds === null && value.max_seconds === null;
  return (
    (value.duration_mode == null || value.duration_mode === "manual") &&
    typeof value.min_seconds === "number" &&
    Number.isFinite(value.min_seconds) &&
    value.min_seconds >= 1 &&
    typeof value.max_seconds === "number" &&
    Number.isFinite(value.max_seconds) &&
    value.max_seconds <= 900 &&
    value.min_seconds <= value.max_seconds
  );
}
export function assertClipContent(plan: ClipPlan, video?: SavedVideo) {
  if (
    !plan?.id ||
    !plan.source ||
    plan.video_id !== plan.source.video_id ||
    !["link", "upload"].includes(plan.source.source_kind) ||
    !["audio", "video"].includes(plan.source.media_type) ||
    !plan.transcript_id ||
    !Number.isInteger(plan.revision) ||
    plan.revision < 1 ||
    !plan.spec?.strategy ||
    !clipStrategyValid(plan.spec.strategy) ||
    !Array.isArray(plan.candidates) ||
    plan.candidates.length > 5 ||
    !["highlights", "no_highlights"].includes(plan.outcome) ||
    plan.candidates.some(
      (item) =>
        !item.id ||
        typeof item.title !== "string" ||
        typeof item.quote !== "string" ||
        typeof item.reason !== "string" ||
        !Number.isFinite(item.start) ||
        !Number.isFinite(item.end) ||
        item.end <= item.start,
    ) ||
    new Set(plan.candidates.map((item) => item.id)).size !==
      plan.candidates.length
  )
    throw new ApiError(
      "CLIP_RESPONSE_INVALID",
      "切片方案数据不完整，请重新加载",
    );
  if (!video) return;
  const same = video.contentId
    ? video.contentId === plan.video_id
    : plan.source.source_kind === "link" &&
      video.metadata.platform === plan.source.platform &&
      video.metadata.platform_video_id === plan.source.platform_video_id;
  if (!same)
    throw new ApiError(
      "CLIP_CONTENT_MISMATCH",
      "切片方案与当前内容不一致，请从任务历史重新打开",
    );
}
export function clipError(cause: unknown): ApiError {
  const error = normalizeError(cause);
  const messages: Record<string, string> = {
    LLM_NOT_CONFIGURED: "精彩片段分析暂不可用，请稍后再试。",
    ASR_NOT_CONFIGURED: "暂时无法获取分析文稿，可使用已有文稿或稍后再试。",
    CLIPS_DISABLED: "切片功能暂未开放，已有候选仍可查看。",
    ALLOWANCES_DISABLED: "切片分析暂不可用，已有候选仍可查看。",
    CLIP_PLAN_NOT_FOUND: "找不到这份切片方案，或当前账号无权查看。",
    CLIP_EXPORT_NOT_FOUND: "找不到这份切片结果，或当前账号无权查看。",
    CLIP_SELECTION_INVALID: "暂时无法找到可靠的候选，可调整条件后重新分析。",
    CLIP_REQUOTE_REQUIRED: "本次确认已失效，请重新获取分析额度。",
    CLIP_SOURCE_REQUIRED:
      "当前没有可用的视频源，请重新上传原文件，或使用已有文稿分析。",
    CLIP_SOURCE_MISMATCH: "来源已发生变化，请重新打开内容后获取分析额度。",
    CLIP_TIMING_NOT_CONFIGURED: "暂时无法生成视频，可先查看候选。",
    CLIP_VIDEO_REQUIRED: "当前来源为音频，生成片段需要真实视频文件。",
    CLIP_TIMING_UNRELIABLE:
      "暂时无法可靠定位所选片段，请减少选择或换其他候选。",
    CLIP_SELECTION_OVERLAP: "所选片段定位后重叠，请调整选择后重新获取报价。",
    CLIP_DURATION_INVALID: "所选片段无法满足时长条件，请调整选择。",
    CLIP_EXPORT_BUDGET_EXCEEDED:
      "所选片段总时长超过处理预算，请减少选择。完整内容不会被自动截断。",
    CLIP_RANGE_UNAVAILABLE: "所选片段范围暂不可用，请调整选择。",
    CLIP_TIMING_BUDGET_EXCEEDED: "本次定位范围较大，请减少所选片段。",
    CLIP_RENDER_LIMIT: "今日视频处理次数已达上限，请稍后再试。",
    CLIP_REVISION_MISMATCH: "方案版本已变化，请重新加载候选后获取报价。",
  };
  return messages[error.code]
    ? new ApiError(
        error.code,
        messages[error.code],
        error.request_id,
        error.status,
      )
    : error;
}
export function assertClipExport(value: ClipExport, plan: ClipPlan) {
  if (
    !value?.id ||
    !value.task_id ||
    value.plan_id !== plan.id ||
    value.revision !== plan.revision ||
    !Array.isArray(value.items) ||
    value.items.length < 1 ||
    value.items.length > 5 ||
    new Set(value.items.map((item) => item.candidate_id)).size !==
      value.items.length ||
    new Set(value.items.map((item) => item.file_id)).size !==
      value.items.length ||
    value.items.some(
      (item) =>
        !item.file_id ||
        !plan.candidates.some(
          (candidate) => candidate.id === item.candidate_id,
        ) ||
        !Number.isFinite(item.start) ||
        !Number.isFinite(item.end) ||
        item.start < 0 ||
        item.end <= item.start ||
        !Number.isFinite(Date.parse(item.expires_at)) ||
        typeof item.download_available !== "boolean" ||
        (item.size_bytes != null &&
          (!Number.isFinite(item.size_bytes) || item.size_bytes < 0)) ||
        (item.verification != null &&
          (!Number.isFinite(item.verification.duration) ||
            item.verification.duration <= 0)),
    )
  )
    throw new ApiError(
      "CLIP_RESPONSE_INVALID",
      "切片成品数据不完整或与当前方案不一致，请重新加载",
    );
}
