import { useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { api } from "../api";
import { normalizeError, type ApiError } from "../api/client";
import { useAuth, authStore } from "../stores/auth";
import { billingStore } from "../stores/billing";
import { useWorkspace } from "../hooks/useWorkspace";
import { terminal } from "../lib/taskTracker";
import type { SavedVideo } from "../stores/workspace";
import type { AllowanceQuote } from "../types/billing";
import type { VideoAnswer, Citation } from "../types/knowledge";
import { timecode } from "../utils";
import AllowanceConfirm from "./AllowanceConfirm";
import ErrorNotice from "./ErrorNotice";
import TaskCard from "./TaskCard";
import Icon from "./Icon";

export function originalLink(url: string, seconds: number) {
  try {
    const parsed = new URL(url);
    if (!["https:", "http:"].includes(parsed.protocol)) return undefined;
    if (
      /(^|\.)youtube\.com$/.test(parsed.hostname) ||
      parsed.hostname === "youtu.be"
    )
      parsed.searchParams.set("t", String(Math.floor(seconds)) + "s");
    else if (/(^|\.)bilibili\.com$/.test(parsed.hostname))
      parsed.searchParams.set("t", String(Math.floor(seconds)));
    return parsed.toString();
  } catch {
    return undefined;
  }
}
export default function VideoQuestions({
  video,
  transcriptId,
  onJump,
}: {
  video: SavedVideo;
  transcriptId: string;
  onJump?: (citation: Citation) => void;
}) {
  const { user } = useAuth();
  return user ? (
    <QuestionSpace
      key={user.id + transcriptId}
      video={video}
      transcriptId={transcriptId}
      userId={user.id}
      onJump={onJump}
    />
  ) : (
    <p className="muted">登录后可针对自己的文稿提问。</p>
  );
}
function QuestionSpace({
  video,
  transcriptId,
  userId,
  onJump,
}: {
  video: SavedVideo;
  transcriptId: string;
  userId: string;
  onJump?: (citation: Citation) => void;
}) {
  const store = useWorkspace();
  const { search } = useLocation();
  const targetId = new URLSearchParams(search).get("answer");
  const [question, setQuestion] = useState(""),
    [answers, setAnswers] = useState<VideoAnswer[]>([]),
    [attempt, setAttempt] = useState(0);
  const [loading, setLoading] = useState(false),
    [submitting, setSubmitting] = useState(false),
    [quote, setQuote] = useState<AllowanceQuote>(),
    [error, setError] = useState<ApiError | null>(null);
  const lock = useRef(false),
    alive = useRef(true),
    request = useRef<AbortController | undefined>(undefined);
  const jobs = store.tasks.filter(
    (task) => task.kind === "ai_question" && task.videoKey === video.key,
  );
  const signature = jobs
    .map(
      (task) =>
        `${task.id}:${task.event.status}:${task.snapshot?.result_payload?.answer_id || ""}`,
    )
    .join("|");
  const running = jobs.find((task) => !terminal(task.event.status));
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      request.current?.abort();
    };
  }, []);
  const current = () =>
    alive.current && authStore.getState().user?.id === userId;
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void Promise.all([
      api.answers(transcriptId, controller.signal),
      targetId
        ? api.answer(targetId, controller.signal)
        : Promise.resolve(null),
    ])
      .then(([data, target]) => {
        if (!controller.signal.aborted) {
          const items =
            target &&
            target.transcript_id === transcriptId &&
            !data.items.some((item) => item.id === target.id)
              ? [target, ...data.items]
              : data.items;
          setAnswers(items);
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(normalizeError(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [transcriptId, signature, attempt, targetId]);
  async function ask() {
    if (lock.current || !question.trim() || !current()) return;
    lock.current = true;
    setSubmitting(true);
    setError(null);
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    try {
      const value = await api.questionQuote(
        transcriptId,
        question.trim(),
        controller.signal,
      );
      if (current()) setQuote(value);
    } catch (cause) {
      if (current()) setError(normalizeError(cause));
    } finally {
      lock.current = false;
      if (current()) setSubmitting(false);
    }
  }
  async function confirm() {
    if (lock.current || !quote || !current()) return;
    lock.current = true;
    setSubmitting(true);
    setError(null);
    try {
      await store.create(
        video,
        "ai_question",
        "视频问答",
        undefined,
        quote.quote_id,
      );
      if (current()) {
        setQuote(undefined);
        setQuestion("");
        void billingStore.refresh();
      }
    } catch (cause) {
      if (current()) setError(normalizeError(cause));
    } finally {
      lock.current = false;
      if (current()) setSubmitting(false);
    }
  }
  return (
    <section className="video-questions" aria-label="针对视频提问">
      <div className="knowledge-section-title">
        <span className="eyebrow">ASK THE VIDEO</span>
        <h3>不止看懂，还能问明白。</h3>
        <p>
          只依据当前视频文稿回答，并附原文。每次独立提问，成功消耗 1 次
          AI；相同版本的已有回答免费。
        </p>
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void ask();
        }}
        className="question-form"
      >
        <label htmlFor={"question-" + transcriptId}>你想进一步了解什么？</label>
        <textarea
          id={"question-" + transcriptId}
          maxLength={1000}
          minLength={2}
          value={question}
          disabled={submitting || !!running || !!quote}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="例如：作者的核心结论是什么，有哪些原文依据？"
          rows={3}
        />
        <div>
          <small>最多 1000 字 · 不使用其他视频或聊天上下文</small>
          <button
            className="button primary"
            disabled={submitting || !!running || question.trim().length < 2}
          >
            <Icon name="chat" size={16} />
            {submitting
              ? "正在处理…"
              : running
                ? "等待本次问答完成"
                : "确认额度并提问"}
          </button>
        </div>
      </form>
      <ErrorNotice error={quote ? null : error}>
        <button
          className="text-button"
          onClick={() => {
            setError(null);
            setAttempt((value) => value + 1);
          }}
        >
          重新加载回答
        </button>
      </ErrorNotice>
      {jobs[0] && <TaskCard task={jobs[0]} />}
      {loading && answers.length === 0 && (
        <p className="muted" role="status">
          正在读取已有回答…
        </p>
      )}
      <div className="answer-list">
        {answers.map((answer) => (
          <article className="answer-card" key={answer.id}>
            <h4>
              <Icon name="chat" size={17} />
              {answer.question}
            </h4>
            <p className="answer-body">{answer.answer}</p>
            {answer.model === "heuristic-v1" && (
              <p className="muted">本地摘录模式，未调用生成式模型。</p>
            )}
            <div className="answer-citations" aria-label="原文依据">
              {answer.citations.map((citation) => (
                <details key={citation.id}>
                  <summary>
                    依据 {citation.id} · {citation.approximate ? "约 " : ""}
                    {timecode(citation.start)}–{timecode(citation.end)}
                  </summary>
                  <blockquote>{citation.quote}</blockquote>
                  <div className="citation-actions">
                    {onJump && (
                      <button
                        className="text-button"
                        onClick={() => onJump(citation)}
                      >
                        查看对应文稿
                      </button>
                    )}
                    {originalLink(video.url, citation.start) && (
                      <a
                        href={originalLink(video.url, citation.start)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        打开原视频 <Icon name="external" size={12} />
                      </a>
                    )}
                  </div>
                </details>
              ))}
            </div>
          </article>
        ))}
      </div>
      {!loading && answers.length === 0 && (
        <p className="question-empty">
          从一个具体问题开始。文稿没有依据时会明确提示，不扣次数。
        </p>
      )}
      {quote && (
        <AllowanceConfirm
          quote={quote}
          label="视频问答"
          pending={submitting}
          error={error}
          onConfirm={() => void confirm()}
          onClose={() => {
            setQuote(undefined);
            setError(null);
          }}
          onRefresh={() => {
            setQuote(undefined);
            void ask();
          }}
        />
      )}
    </section>
  );
}
