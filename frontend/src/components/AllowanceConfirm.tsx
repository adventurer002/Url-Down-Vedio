import {
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { Link } from "react-router-dom";
import type { ApiError } from "../api/client";
import type { AllowanceQuote } from "../types/billing";
import { useBilling } from "../stores/billing";
import { durationLabel } from "../utils/billing";
import ErrorNotice from "./ErrorNotice";
import Icon from "./Icon";

export default function AllowanceConfirm({
  quote,
  label,
  pending,
  error,
  onConfirm,
  onClose,
  onRefresh,
  sourceNote,
  inline = false,
  confirmationUncertain = false,
  requiresRefresh = false,
  details,
  returnFocusRef,
}: {
  quote: AllowanceQuote;
  label: string;
  pending: boolean;
  error: ApiError | null;
  onConfirm: () => void;
  onClose: () => void;
  onRefresh: () => void;
  sourceNote?: string;
  inline?: boolean;
  confirmationUncertain?: boolean;
  requiresRefresh?: boolean;
  details?: ReactNode;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const dialog = useRef<HTMLDialogElement>(null),
    titleId = useId(),
    billing = useBilling();
  const [clock, setClock] = useState(Date.now());
  useEffect(() => {
    const previous =
      returnFocusRef?.current || (document.activeElement as HTMLElement | null);
    if (!inline) dialog.current?.showModal();
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => {
      clearInterval(timer);
      if (!inline) {
        dialog.current?.close();
        if (previous?.isConnected && !previous.closest("[hidden]"))
          previous.focus();
      }
    };
  }, [inline, returnFocusRef]);
  const expired = requiresRefresh || clock >= Date.parse(quote.expires_at),
    maximum = quote.maximum;
  const insufficient =
    !!billing.data &&
    (billing.data.balances.asr_seconds.available < maximum.asr_seconds ||
      billing.data.balances.ai_generations.available < maximum.ai_generations);
  const ai = quote.task_type.startsWith("ai_"),
    free = !maximum.asr_seconds && !maximum.ai_generations;
  const exporting = quote.task_type === "clips_export";
  const content = (
    <>
      {!inline && (
        <button
          className="dialog-close"
          aria-label="关闭额度确认"
          disabled={pending}
          onClick={onClose}
        >
          <Icon name="close" />
        </button>
      )}
      {!inline && (
        <span className="billing-dialog-icon">
          <Icon name={ai ? "sparkles" : "download"} size={25} />
        </span>
      )}
      <h2 id={titleId}>
        {exporting
          ? "确认生成视频"
          : free && ai
            ? "免费取回已有结果"
            : "确认本次额度"}
      </h2>
      <p>
        {label}
        {quote.task_type === "ai_mindmap" ? " · 包含总结与导图" : ""}
      </p>
      {details}
      {sourceNote && (
        <p className="quote-source-note">
          <Icon name="text" size={16} />
          {sourceNote}
        </p>
      )}
      <div className="quote-amounts">
        <div>
          <span>最多占用转写额度</span>
          <strong>{durationLabel(maximum.asr_seconds)}</strong>
        </div>
        <div>
          <span>最多占用 AI 额度</span>
          <strong>{maximum.ai_generations} 次</strong>
        </div>
      </div>
      {insufficient && (
        <div className="quote-warning">
          当前额度不足，确认后任务将等待补充额度。
          <Link to="/account" onClick={onClose}>
            查看额度
          </Link>
          <Link to="/subscription" onClick={onClose}>
            查看订阅方案
          </Link>
        </div>
      )}
      <p>
        {exporting
          ? "本次不消耗转写和 AI 次数，仍受每日处理次数及下载流量限制。"
          : ["download", "audio_extraction"].includes(quote.task_type)
            ? "下载按当前免费次数或会员权益执行，不消耗转写和 AI 额度。"
            : quote.task_type === "ai_question"
              ? "使用当前文稿回答，不重复转写。相同版本的已有回答免费；新的问答成功后扣 1 次 AI，原文依据不足时不扣次数。"
              : quote.task_type === "ai_clip_plan"
                ? "寻找能独立表达的精彩观点，实际数量可能少于设置值。没有符合条件的片段时，本次分析不扣 AI 额度。"
                : "排队不占额度，开始执行时占用上限；已有字幕或转写缓存不扣分钟，成功后按实际使用核销并释放未使用部分。"}
      </p>
      <p className="task-confirmation-note">
        {exporting
          ? "实际开始处理后，失败或取消也可能占用处理次数。确认后关闭页面不影响后台处理。"
          : "失败或有效取消不扣额度，自动重试不重复核销。确认后关闭页面不影响后台处理。"}
      </p>
      {exporting && maximum.clip_limits && (
        <p className="task-confirmation-note">
          每日最多处理 {maximum.clip_limits.max_daily_exports}{" "}
          批，同一方案每日最多 {maximum.clip_limits.max_daily_plan_exports}{" "}
          批；以服务端准入为准。
        </p>
      )}
      {!sourceNote && quote.task_type !== "ai_question" && !exporting && (
        <p className="task-confirmation-note">
          本次内容最长 {maximum.limits.video_seconds / 60} 分钟，文件上限{" "}
          {maximum.limits.file_bytes / 1024 ** 2} MiB。排队执行前重新核验权益。
        </p>
      )}
      <p className="quote-expiry">
        {confirmationUncertain
          ? "上次提交结果尚未确认，请继续检查原任务，避免重复新建。"
          : requiresRefresh
            ? "本次确认已失效，请重新获取额度。"
            : expired
              ? "本次报价已过期，请重新获取。"
              : `本次确认有效至 ${new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit" }).format(new Date(quote.expires_at))}（北京时间）`}
      </p>
      <ErrorNotice error={error} />
      <div className="quote-actions">
        <button
          className="button secondary"
          disabled={pending}
          onClick={onClose}
        >
          暂不开始
        </button>
        <button
          className="button primary"
          disabled={pending}
          onClick={expired && !confirmationUncertain ? onRefresh : onConfirm}
        >
          {pending
            ? "正在提交…"
            : confirmationUncertain
              ? "继续检查原任务"
              : expired
                ? "重新获取报价"
                : insufficient
                  ? "确认并排队"
                  : exporting
                    ? "确认并生成"
                    : free && ai
                      ? "免费取回结果"
                      : "确认并开始"}
        </button>
      </div>
    </>
  );
  return inline ? (
    <section className="quote-inline" aria-labelledby={titleId}>
      {content}
    </section>
  ) : (
    <dialog
      ref={dialog}
      className="billing-dialog quote-dialog"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        if (!pending) onClose();
      }}
    >
      {content}
    </dialog>
  );
}
