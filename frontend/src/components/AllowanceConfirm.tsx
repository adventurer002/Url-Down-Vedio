import { useEffect, useRef, useState } from "react";
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
}: {
  quote: AllowanceQuote;
  label: string;
  pending: boolean;
  error: ApiError | null;
  onConfirm: () => void;
  onClose: () => void;
  onRefresh: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const billing = useBilling();
  const [clock, setClock] = useState(Date.now());
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => {
      clearInterval(timer);
      dialog.current?.close();
      previous?.focus();
    };
  }, []);
  const expired = clock >= new Date(quote.expires_at).getTime();
  const maximum = quote.maximum;
  const insufficient =
    billing.data &&
    (billing.data.balances.asr_seconds.available < maximum.asr_seconds ||
      billing.data.balances.ai_generations.available < maximum.ai_generations);
  const ai = quote.task_type.startsWith("ai_");
  return (
    <dialog
      ref={dialog}
      className="billing-dialog quote-dialog"
      aria-labelledby="quote-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!pending) onClose();
      }}
    >
      <button
        className="dialog-close"
        aria-label="关闭额度确认"
        disabled={pending}
        onClick={onClose}
      >
        <Icon name="close" />
      </button>
      <span className="billing-dialog-icon">
        <Icon name={ai ? "sparkles" : "download"} size={25} />
      </span>
      <p className="eyebrow">BEFORE WE START</p>
      <h2 id="quote-title">确认本次额度</h2>
      <p>
        {label}
        {quote.task_type === "ai_mindmap" ? " · 包含总结与导图" : ""}
      </p>
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
          <Link to="/subscription">查看套餐与额度 →</Link>
        </div>
      )}
      <p>
        {quote.task_type === "download" ||
        quote.task_type === "audio_extraction"
          ? "下载按当前免费次数或会员权益执行，不消耗转写和 AI 额度。"
          : quote.task_type === "ai_question"
            ? "使用已获得的当前视频文稿，不重复转写。相同版本的已有回答免费取回；新的问答成功后扣 1 次 AI，原文依据不足时不扣次数。"
            : "排队不占额度，开始执行时占用上限；已有字幕或转写缓存不扣分钟，成功后释放未使用部分。"}
      </p>
      <p className="task-confirmation-note">
        失败或有效取消不扣额度，自动重试不重复核销。确认后关闭页面不影响任务。
      </p>
      {quote.task_type !== "ai_question" && (
        <p className="task-confirmation-note">
          本次视频最长 {maximum.limits.video_seconds / 60} 分钟，文件上限{" "}
          {maximum.limits.file_bytes / 1024 ** 2} MiB， 每日下载流量上限{" "}
          {maximum.limits.daily_egress_bytes / 1024 ** 3} GiB。
          下载按免费每日次数或会员权益核销；排队执行前重新核验权益。
        </p>
      )}
      <p className="quote-expiry">
        {expired
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
          onClick={expired ? onRefresh : onConfirm}
        >
          {pending
            ? "正在提交…"
            : expired
              ? "重新获取报价"
              : insufficient
                ? "确认并排队"
                : "确认并开始"}
        </button>
      </div>
    </dialog>
  );
}
