import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { authStore, useAuth } from "../../stores/auth";
import { useWorkspace } from "../../hooks/useWorkspace";
import type { ClipWorkspaceResource } from "../../hooks/useClipWorkspace";
import type { SavedVideo } from "../../stores/workspace";
import type { ClipStrategy, ClipPlan } from "../../types/clips";
import type { AllowanceQuote } from "../../types/billing";
import { ApiError } from "../../api/client";
import { api } from "../../api";
import { billingStore } from "../../stores/billing";
import {
  clipError,
  clipStrategyValid,
  defaultClipStrategy,
  latestClipAnalysis,
} from "../../lib/clips";
import { processingInput, sourceExpired } from "../../lib/content";
import { terminal } from "../../lib/taskTracker";
import AllowanceConfirm from "../AllowanceConfirm";
import ErrorNotice from "../ErrorNotice";
import Icon from "../Icon";
import TaskCard from "../TaskCard";
import ClipStrategyForm from "./ClipStrategyForm";
import ClipExportWorkspace from "./ClipExportWorkspace";

type Proposal = { quote: AllowanceQuote; text: boolean };
export default function ClipWorkspace({
  video,
  transcriptId,
  resource,
  active,
  creationEnabled,
  onTask,
  onPlan,
}: {
  video: SavedVideo;
  transcriptId?: string;
  resource: ClipWorkspaceResource;
  active: boolean;
  creationEnabled: boolean;
  onTask: (id: string) => void;
  onPlan: (plan: ClipPlan) => void;
}) {
  const store = useWorkspace(),
    auth = useAuth(),
    plan = resource.plan;
  const sourceTranscript = plan?.transcript_id || transcriptId;
  const [strategy, setStrategy] = useState<ClipStrategy>(() =>
    defaultClipStrategy(video.metadata.duration),
  );
  const [settings, setSettings] = useState(true),
    [proposal, setProposal] = useState<Proposal>();
  const [quoting, setQuoting] = useState(false),
    [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<ApiError | null>(null),
    [quoteError, setQuoteError] = useState<ApiError | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const generation = useRef(0),
    request = useRef<AbortController | null>(null),
    lock = useRef(false);
  const owner = JSON.stringify([
    auth.user?.id,
    auth.token,
    video.key,
    sourceTranscript,
  ]);
  const currentOwner = useRef(owner),
    visible = useRef(active),
    alive = useRef(true);
  currentOwner.current = owner;
  visible.current = active;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      generation.current++;
      request.current?.abort();
    };
  }, []);
  useEffect(() => {
    generation.current++;
    request.current?.abort();
    setProposal(undefined);
    setQuoting(false);
    setQuoteError(null);
    setError(null);
    setUnavailable(false);
  }, [owner, active]);
  useEffect(() => {
    lock.current = false;
    setConfirming(false);
  }, [owner]);
  useEffect(() => {
    if (plan) {
      setSettings(false);
      const previous = plan.spec.strategy;
      setStrategy(
        previous.duration_mode
          ? previous
          : {
              ...previous,
              duration_mode: "auto",
              min_seconds: null,
              max_seconds: null,
            },
      );
    }
  }, [plan?.id]);
  const running = store.tasks.find(
    (task) =>
      task.videoKey === video.key &&
      task.kind === "ai_clip_plan" &&
      !terminal(task.event.status),
  );
  const latestAnalysis = latestClipAnalysis(
    store.tasks.filter((task) => task.videoKey === video.key),
  );
  const lastFailed =
    latestAnalysis?.event.status === "failed" ? latestAnalysis : undefined;
  const cached =
    latestAnalysis?.event.status === "completed" &&
    latestAnalysis.snapshot?.result_payload?.cache_hit &&
    latestAnalysis.snapshot.result_payload.clip_plan_id === plan?.id;
  const pendingId = video.pendingClipQuoteId;
  const sourceReady =
    !!sourceTranscript ||
    (video.source?.kind === "upload"
      ? !!video.source.uploadId &&
        video.source.status === "ready" &&
        !sourceExpired(video)
      : !!video.url);
  const enabled = creationEnabled && !unavailable;
  const valid = (expected: string, epoch?: number) =>
    alive.current &&
    currentOwner.current === expected &&
    authStore.getState().user?.id === auth.user?.id &&
    authStore.getState().token === auth.token &&
    (epoch === undefined || epoch === generation.current);
  function change(next: ClipStrategy) {
    generation.current++;
    request.current?.abort();
    setProposal(undefined);
    setQuoting(false);
    setQuoteError(null);
    setStrategy(next);
  }
  async function quote() {
    if (
      !active ||
      !enabled ||
      quoting ||
      lock.current ||
      pendingId ||
      running ||
      !clipStrategyValid(strategy) ||
      !authStore.requireLogin()
    )
      return;
    if (store.userId !== auth.user?.id) return;
    request.current?.abort();
    const controller = new AbortController(),
      epoch = ++generation.current,
      expected = owner;
    request.current = controller;
    setQuoting(true);
    setError(null);
    setProposal(undefined);
    setQuoteError(null);
    try {
      const input = processingInput(video, "ai_clip_plan", sourceTranscript);
      const result = await api.clipPlanQuote(
        {
          ...input,
          strategy: { ...strategy },
          language: plan?.spec.language || "zh",
        },
        controller.signal,
      );
      if (
        valid(expected, epoch) &&
        visible.current &&
        !controller.signal.aborted
      ) {
        if (result.task_type !== "ai_clip_plan")
          throw new ApiError("QUOTE_MISMATCH", "分析额度与操作不一致，请重试");
        setProposal({ quote: result, text: !!input.transcript_id });
      }
    } catch (cause) {
      if (valid(expected, epoch) && !controller.signal.aborted) {
        const problem = clipError(cause);
        setError(problem);
        if (["CLIPS_DISABLED", "ALLOWANCES_DISABLED"].includes(problem.code))
          setUnavailable(true);
      }
    } finally {
      if (valid(expected, epoch)) setQuoting(false);
    }
  }
  async function confirm(recover = false) {
    if (
      lock.current ||
      !authStore.requireLogin() ||
      store.userId !== auth.user?.id
    )
      return;
    const quoteId = recover
      ? video.pendingClipQuoteId
      : proposal?.quote.quote_id;
    if (!quoteId) return;
    lock.current = true;
    setConfirming(true);
    setQuoteError(null);
    setError(null);
    const expected = owner;
    try {
      const task = recover
        ? await store.recoverClipConfirmation(video)
        : await store.create(
            video,
            "ai_clip_plan",
            "寻找精彩片段",
            undefined,
            quoteId,
          );
      if (valid(expected)) {
        setProposal(undefined);
        if (visible.current && !plan) onTask(task.id);
        void billingStore.refresh();
      }
    } catch (cause) {
      if (valid(expected)) {
        const problem = clipError(cause);
        if (recover) setError(problem);
        else setQuoteError(problem);
      }
    } finally {
      if (currentOwner.current === expected) lock.current = false;
      if (valid(expected)) setConfirming(false);
    }
  }
  const disabled =
    quoting || confirming || !!running || !!pendingId || !sourceReady;
  const uncertain = !!pendingId && !confirming;
  return (
    <div className="clip-workspace">
      <header className="content-panel-heading">
        <div>
          <h2>精彩切片</h2>
          <p>先从原文中找到能独立表达的精彩观点。</p>
        </div>
        {plan && enabled && (
          <button
            className="text-button"
            disabled={confirming || !!pendingId}
            onClick={() => setSettings(!settings)}
          >
            {settings ? "返回候选" : "调整查找条件"}
          </button>
        )}
      </header>
      <div className="clip-body">
        <ErrorNotice error={resource.error}>
          <button className="text-button" onClick={resource.reload}>
            重新加载候选
          </button>
        </ErrorNotice>
        <ErrorNotice error={error} />
        {resource.loading && !plan && (
          <p className="clip-loading" role="status">
            正在读取切片方案…
          </p>
        )}
        {!auth.user && (
          <div className="clip-note">
            登录后可寻找精彩片段或查看已有方案。
            <button
              className="text-button"
              onClick={() => authStore.requireLogin()}
            >
              登录
            </button>
          </div>
        )}
        {(!creationEnabled || unavailable) && (
          <p className="clip-note">切片分析暂未开放，已有候选仍可查看。</p>
        )}
        {uncertain && (
          <div className="clip-note" role="status">
            <p>上次提交的结果尚未确认，请继续检查原任务，避免重复新建。</p>
            <button
              className="button secondary"
              disabled={confirming}
              onClick={() => void confirm(true)}
            >
              继续检查原任务
            </button>
            <button
              className="text-button"
              onClick={() => store.setHistoryOpen(true)}
            >
              打开任务历史
            </button>
          </div>
        )}
        {running && <TaskCard task={running} />}
        {!running && lastFailed && <TaskCard task={lastFailed} />}
        {resource.pending && (
          <div className="clip-new-result" role="status">
            <span>新的候选已准备好，当前阅读位置已保留。</span>
            <button
              className="text-button"
              onClick={() => {
                const next = resource.acceptPending();
                if (next) onPlan(next);
              }}
            >
              查看新候选
            </button>
          </div>
        )}
        {enabled && auth.user && !resource.loading && (settings || !plan) && (
          <>
            {!sourceReady && (
              <p className="clip-note">
                暂无可用来源。可以先从文稿分区获取文稿，或重新上传原文件。
              </p>
            )}
            <ClipStrategyForm
              value={strategy}
              onChange={change}
              onQuote={() => void quote()}
              disabled={disabled || store.userId !== auth.user.id}
              quoting={quoting}
              textSource={!!sourceTranscript}
            />
          </>
        )}
        {plan && !settings && (
          <>
            {cached && (
              <p className="clip-note" role="status">
                已取回已有候选，本次未扣 AI 额度。
              </p>
            )}
            {plan.mock && (
              <p className="clip-note">当前为示例分析结果，请核对原文。</p>
            )}
            {plan.source_status === "audio_only" && (
              <p className="clip-note">
                当前来源为音频，可阅读和复制精彩段落，视频导出需要真实视频文件。
              </p>
            )}
            {plan.source_status === "source_required" && (
              <p className="clip-note">原视频暂不可用，候选原文仍可查看。</p>
            )}
            {plan.outcome === "no_highlights" ? (
              <div className="clip-empty">
                <Icon name="clips" size={30} />
                <h3>没有找到符合当前条件的片段</h3>
                <p>
                  {plan.reason ||
                    "内容可能需要更多上下文，可以调整时长或关注方向后再试。"}
                </p>
                <p>本次分析不扣 AI 额度。</p>
                <Link
                  className="text-button"
                  to={`/video/${encodeURIComponent(video.key)}?${new URLSearchParams({ tab: "transcript", transcript: plan.transcript_id, clip_plan: plan.id })}`}
                >
                  查看对应文稿
                </Link>
              </div>
            ) : (
              <>
                <div className="clip-list-heading">
                  <strong>找到 {plan.candidates.length} 段候选</strong>
                  <span>时间仅供参考，生成视频时需精确定位。</span>
                </div>
              </>
            )}
            <ClipExportWorkspace
              key={`${plan.id}:${plan.revision}`}
              plan={plan}
              video={video}
              active={active}
              enabled={enabled}
              initial={resource.exported}
            />
          </>
        )}
      </div>
      {proposal && active && (
        <AllowanceConfirm
          quote={proposal.quote}
          label="寻找精彩片段"
          sourceNote={
            proposal.text ? "使用已有文稿，无需重新转写。" : undefined
          }
          pending={confirming}
          error={quoteError}
          confirmationUncertain={uncertain}
          requiresRefresh={
            !!quoteError &&
            [
              "CLIP_REQUOTE_REQUIRED",
              "QUOTE_EXPIRED",
              "QUOTE_MISMATCH",
              "QUOTE_NOT_FOUND",
              "TRANSCRIPT_SOURCE_MISMATCH",
            ].includes(quoteError.code)
          }
          onConfirm={() => void confirm(!!pendingId)}
          onClose={() => {
            if (!lock.current) setProposal(undefined);
          }}
          onRefresh={() => void quote()}
        />
      )}
    </div>
  );
}
