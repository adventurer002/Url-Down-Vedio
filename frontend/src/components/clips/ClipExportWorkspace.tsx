import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { ApiError } from "../../api/client";
import { authStore, useAuth } from "../../stores/auth";
import { useWorkspace } from "../../hooks/useWorkspace";
import { useClipDelivery } from "../../hooks/useClipDelivery";
import { clipError, compareClipAnalyses } from "../../lib/clips";
import { terminal } from "../../lib/taskTracker";
import type {
  ClipPlan,
  ClipExport,
  ClipExportQuoteInput,
} from "../../types/clips";
import type { SavedVideo } from "../../stores/workspace";
import type { AllowanceQuote } from "../../types/billing";
import AllowanceConfirm from "../AllowanceConfirm";
import TaskCard from "../TaskCard";
import ErrorNotice from "../ErrorNotice";
import ClipCandidateList from "./ClipCandidateList";
import ClipDeliveryCards from "./ClipDeliveryCards";

type Proposal = {
  owner: string;
  quote: AllowanceQuote;
  input: ClipExportQuoteInput;
};
export default function ClipExportWorkspace({
  video,
  plan,
  initial,
  active,
  enabled,
}: {
  video: SavedVideo;
  plan: ClipPlan;
  initial?: ClipExport;
  active: boolean;
  enabled: boolean;
}) {
  const store = useWorkspace(),
    auth = useAuth();
  const owner = JSON.stringify([
    auth.user?.id,
    auth.token,
    video.key,
    plan.id,
    plan.revision,
  ]);
  const currentOwner = useRef(owner),
    alive = useRef(true),
    lock = useRef(false),
    generation = useRef(0);
  currentOwner.current = owner;
  const request = useRef<AbortController | null>(null);
  const review = useRef<HTMLDivElement | null>(null);
  const quoteButton = useRef<HTMLButtonElement | null>(null);
  const [reading, setReading] = useState(false);
  const initialIds = () =>
    video.clipSelection?.plan_id === plan.id &&
    video.clipSelection.revision === plan.revision
      ? video.clipSelection.candidate_ids.filter((id) =>
          plan.candidates.some((candidate) => candidate.id === id),
        )
      : [];
  const [draft, setDraft] = useState({ owner, ids: initialIds() });
  const ids = draft.owner === owner ? draft.ids : initialIds();
  const [proposal, setProposal] = useState<Proposal>();
  const [quoting, setQuoting] = useState(false),
    [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<ApiError | null>(null),
    [quoteError, setQuoteError] = useState<ApiError | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const result = useClipDelivery(
    plan,
    video.key,
    initial?.plan_id === plan.id ? initial : undefined,
    active,
  );
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      request.current?.abort();
    };
  }, []);
  useEffect(() => {
    const list = review.current?.querySelector(".clip-candidates");
    if (!list || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      ([entry]) =>
        setReading(entry.isIntersecting && entry.intersectionRatio >= 0.15),
      { threshold: [0, 0.15] },
    );
    observer.observe(list);
    return () => observer.disconnect();
  }, [plan.id]);
  useEffect(() => {
    generation.current++;
    request.current?.abort();
    setProposal(undefined);
    setQuoting(false);
    setError(null);
    setQuoteError(null);
  }, [owner, active]);
  useEffect(() => {
    lock.current = false;
    setConfirming(false);
    setUnavailable(false);
    setDraft({ owner, ids: initialIds() });
  }, [owner]);
  const valid = (expected: string, epoch?: number) =>
    alive.current &&
    currentOwner.current === expected &&
    authStore.getState().user?.id === auth.user?.id &&
    authStore.getState().token === auth.token &&
    store.userId === auth.user?.id &&
    (epoch == null || generation.current === epoch);
  const exports = store.tasks
    .filter(
      (task) =>
        task.kind === "clips_export" &&
        (task.videoKey === video.key ||
          task.snapshot?.video_id === plan.video_id),
    )
    .sort((a, b) => compareClipAnalyses(b, a));
  const running = exports.find((task) => !terminal(task.event.status));
  const latest = exports[0];
  const lastFailed =
    latest && ["failed", "cancelled", "expired"].includes(latest.event.status)
      ? latest
      : undefined;
  const pending = video.pendingClipExport;
  const videoSource = plan.source.media_type === "video";
  const locked = confirming || !!pending || !!running;
  const creation =
    enabled && !unavailable && videoSource && plan.candidates.length > 0;
  const inputFor = (candidateIds: string[]): ClipExportQuoteInput => ({
    plan_id: plan.id,
    revision: plan.revision,
    candidate_ids: [...candidateIds].sort(),
    profile: "video_720_mp4",
  });
  function change(candidateIds: string[]) {
    if (locked) return;
    generation.current++;
    request.current?.abort();
    setQuoting(false);
    setProposal(undefined);
    setQuoteError(null);
    setError(null);
    const next = plan.candidates
      .filter((candidate) => candidateIds.includes(candidate.id))
      .map((candidate) => candidate.id);
    setDraft({ owner, ids: next });
    store.setClipSelection(video.key, inputFor(next));
  }
  async function quote() {
    if (
      !active ||
      !creation ||
      locked ||
      quoting ||
      !ids.length ||
      !authStore.requireLogin() ||
      store.userId !== auth.user?.id
    )
      return;
    request.current?.abort();
    const controller = new AbortController(),
      epoch = ++generation.current,
      expected = owner,
      input = inputFor(ids);
    request.current = controller;
    setQuoting(true);
    setError(null);
    setQuoteError(null);
    setProposal(undefined);
    try {
      const quoted = await api.clipExportQuote(input, controller.signal);
      if (!valid(expected, epoch) || controller.signal.aborted) return;
      if (quoted.task_type !== "clips_export")
        throw new ApiError(
          "QUOTE_MISMATCH",
          "生成报价与操作不一致，请重新获取",
        );
      setProposal({ owner: expected, quote: quoted, input });
    } catch (cause) {
      if (valid(expected, epoch) && !controller.signal.aborted) {
        const problem = clipError(cause);
        setError(problem);
        if (
          [
            "CLIPS_DISABLED",
            "ALLOWANCES_DISABLED",
            "CLIP_TIMING_NOT_CONFIGURED",
          ].includes(problem.code)
        )
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
    const frozen = proposal?.owner === owner ? proposal : undefined;
    if (recover ? !pending : !frozen) return;
    const expected = owner;
    lock.current = true;
    setConfirming(true);
    setQuoteError(null);
    setError(null);
    try {
      if (recover) await store.recoverClipExport(video);
      else
        await store.confirmClipExport(
          video,
          frozen!.quote.quote_id,
          frozen!.input,
        );
      if (valid(expected)) setProposal(undefined);
    } catch (cause) {
      if (valid(expected)) {
        if (recover) setError(clipError(cause));
        else setQuoteError(clipError(cause));
      }
    } finally {
      if (currentOwner.current === expected) lock.current = false;
      if (valid(expected)) setConfirming(false);
    }
  }
  const shownProposal = proposal?.owner === owner ? proposal : undefined;
  return (
    <>
      {pending && !confirming && (
        <div className="clip-note" role="status">
          <p>
            上次视频生成的提交结果尚未确认，请继续检查原任务，避免重复新建。
          </p>
          <button
            className="button secondary"
            onClick={() => void confirm(true)}
          >
            继续检查视频生成
          </button>
        </div>
      )}
      {running && <TaskCard task={running} />}
      {!running && lastFailed && <TaskCard task={lastFailed} />}
      <ErrorNotice error={error} />
      <div
        ref={review}
        className={`clip-review-selection${reading ? " reading" : ""}`}
      >
        <ClipCandidateList
          plan={plan}
          contentKey={video.key}
          active={active}
          selection={
            creation ? { ids, disabled: locked, onChange: change } : undefined
          }
        />
        {unavailable && (
          <p className="clip-note">
            视频生成暂不可用，候选和已有成品仍可查看。
          </p>
        )}
        {creation && (
          <div className="clip-selection-bar">
            <div>
              <strong>已选择 {ids.length} 段</strong>
              <p>最终时长在生成时核对，输出 MP4 / 最高 720p / 保留原比例。</p>
            </div>
            <button
              ref={quoteButton}
              className="button primary"
              disabled={!ids.length || locked || quoting || !active}
              onClick={() => void quote()}
            >
              {quoting ? "正在获取生成说明…" : "查看生成说明"}
            </button>
          </div>
        )}
      </div>
      <ClipDeliveryCards
        result={result}
        plan={plan}
        videoKey={video.key}
        active={active}
      />
      {shownProposal && active && (
        <AllowanceConfirm
          quote={shownProposal.quote}
          label={`生成 ${shownProposal.input.candidate_ids.length} 段视频`}
          pending={confirming}
          error={quoteError}
          returnFocusRef={quoteButton}
          confirmationUncertain={!!pending && !confirming}
          requiresRefresh={
            !!quoteError &&
            [
              "CLIP_REQUOTE_REQUIRED",
              "QUOTE_EXPIRED",
              "QUOTE_MISMATCH",
              "QUOTE_NOT_FOUND",
              "CLIP_REVISION_MISMATCH",
              "CLIP_SOURCE_MISMATCH",
            ].includes(quoteError.code)
          }
          details={
            <div className="clip-export-summary">
              <ul>
                {plan.candidates
                  .filter((candidate) =>
                    shownProposal.input.candidate_ids.includes(candidate.id),
                  )
                  .map((candidate) => (
                    <li key={candidate.id}>{candidate.title}</li>
                  ))}
              </ul>
              <p>MP4 · 最高 720p · 保留原比例</p>
            </div>
          }
          onConfirm={() => void confirm(!!pending)}
          onClose={() => {
            if (!lock.current) setProposal(undefined);
          }}
          onRefresh={() => void quote()}
        />
      )}
    </>
  );
}
