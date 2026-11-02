import type { ApiError } from "../api/client";

export function errorPresentation(error: Pick<ApiError, "code" | "message">) {
  const { code, message } = error;
  if (
    [
      "UPLOAD_NO_AUDIO",
      "UPLOAD_FORMAT_MISMATCH",
      "UPLOAD_INVALID_MEDIA",
    ].includes(code)
  )
    return {
      message:
        code === "UPLOAD_NO_AUDIO"
          ? "这个文件没有可识别的音轨"
          : "暂时无法读取这个媒体文件",
      suggestion:
        "请确认文件可以正常播放，并选择带音轨的视频或 MP3、M4A 音频重新上传。",
    };
  if (["UPLOAD_EXPIRED", "UPLOAD_FILE_MISSING"].includes(code))
    return {
      message: "原文件已到期或暂时不可用",
      suggestion:
        "已有文稿和 AI 成果仍可使用；需要重新识别时，可重新上传文件。",
    };
  if (["STORAGE_QUOTA_EXCEEDED", "INSUFFICIENT_DISK_SPACE"].includes(code))
    return {
      message: "当前存储空间不足",
      suggestion: "可以删除不再需要的原文件，或等待过期文件清理后再试。",
    };
  if (
    code === "QUESTION_INSUFFICIENT_EVIDENCE" ||
    code === "INSUFFICIENT_EVIDENCE" ||
    code === "QUESTION_NO_EVIDENCE"
  )
    return {
      message: "文稿中未找到足够依据",
      suggestion: "可以换一种问法。本次未消耗 AI 额度。",
    };
  if (code === "DATABASE_UNAVAILABLE")
    return {
      message: "服务暂时未能处理这次请求",
      suggestion: "请稍后重试；若持续出现，请联系管理员。",
    };
  if (code === "NETWORK_ERROR")
    return {
      message: "暂时无法连接服务",
      suggestion: "请检查网络连接后重试；如果仍无法连接，可以稍后再试。",
    };
  if (code.endsWith("_TIMEOUT"))
    return {
      message: "本次处理未能及时完成",
      suggestion: "可以先查看任务历史确认处理状态，再决定是否重新尝试。",
    };
  if (
    [
      "ASR_NOT_CONFIGURED",
      "LLM_NOT_CONFIGURED",
      "FFMPEG_UNAVAILABLE",
      "YTDLP_RUNTIME_UNAVAILABLE",
      "AUTH_MISCONFIGURED",
    ].includes(code)
  )
    return {
      message: "这项功能暂时不可用",
      suggestion: "可以先使用其他功能；若持续不可用，请联系管理员。",
    };
  if (
    ["SOURCE_RATE_LIMITED", "ASR_RATE_LIMITED", "QUOTE_RATE_LIMITED"].includes(
      code,
    )
  )
    return {
      message: "请求较多，请稍后再试",
      suggestion: "等待一会儿再操作，避免连续重复提交。",
    };
  if (code === "SOURCE_ACCESS_RESTRICTED")
    return {
      message: "视频平台暂时限制了服务器访问",
      suggestion: "请稍后重试；也可以上传你有权使用的视频文件继续处理。",
    };
  if (
    [
      "SOURCE_UNAVAILABLE",
      "SOURCE_NETWORK_UNAVAILABLE",
      "DOWNLOAD_FAILED",
      "PARSE_FAILED",
    ].includes(code)
  )
    return {
      message: "暂时无法获取视频内容",
      suggestion: "请确认原视频仍可访问，再重试或换一个公开的视频链接。",
    };
  if (
    [
      "ASR_API_ERROR",
      "LLM_API_ERROR",
      "ASR_INVALID_RESPONSE",
      "LLM_INVALID_RESPONSE",
    ].includes(code)
  )
    return {
      message: "内容处理暂时未能完成",
      suggestion: "请稍后重试；如果多次失败，可以联系管理员协助处理。",
    };
  if (code === "WORKER_LOST")
    return {
      message: "任务执行意外中断",
      suggestion: "请先查看任务状态，确认后再从对应功能重新提交。",
    };
  return {
    message: /[\u3400-\u9fff]/.test(message) ? message : "本次操作未能完成",
    suggestion: "",
  };
}
