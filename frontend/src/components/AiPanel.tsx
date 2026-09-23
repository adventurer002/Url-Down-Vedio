import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiRequestError } from "../api/client";
import { ErrorNote } from "./VideoCard";
import {
  getMindmap,
  getSummary,
  getTranscript,
  triggerAi,
} from "../api/videos";

type AiKind = "audio" | "transcribe" | "summarize" | "mindmap";

const AI_BUTTONS: { kind: AiKind; label: string; hint: string }[] = [
  { kind: "audio", label: "提取音频", hint: "MP3" },
  { kind: "transcribe", label: "转写字幕", hint: "ASR" },
  { kind: "summarize", label: "AI 总结", hint: "需先转写" },
  { kind: "mindmap", label: "思维导图", hint: "需先总结" },
];

export function AiPanel({ videoId }: { videoId: string }) {
  const qc = useQueryClient();
  const [tasks, setTasks] = useState<Partial<Record<AiKind, string>>>({});
  const [note, setNote] = useState<{ code: string; message: string } | null>(null);

  const trigger = useMutation({
    mutationFn: (kind: AiKind) => triggerAi(videoId, kind),
    onSuccess: (data, kind) => {
      setNote(null);
      if (data.task_id) {
        setTasks((t) => ({ ...t, [kind]: data.task_id }));
      }
      void qc.invalidateQueries({ queryKey: ["ai", videoId] });
    },
    onError: (e) => {
      if (e instanceof ApiRequestError) setNote({ code: e.code, message: e.message });
    },
  });

  const outputs = useQuery({
    queryKey: ["ai", videoId, tasks],
    queryFn: async () => {
      const out: {
        transcript?: Awaited<ReturnType<typeof getTranscript>>;
        summary?: Awaited<ReturnType<typeof getSummary>>;
        mindmap?: Awaited<ReturnType<typeof getMindmap>>;
      } = {};
      try {
        out.transcript = await getTranscript(videoId);
      } catch {
        /* not ready yet */
      }
      try {
        out.summary = await getSummary(videoId);
      } catch {
        /* not ready yet */
      }
      try {
        out.mindmap = await getMindmap(videoId);
      } catch {
        /* not ready yet */
      }
      return out;
    },
    refetchInterval: 3000,
  });

  return (
    <div className="card space-y-4 p-5">
      <div>
        <p className="text-sm font-semibold">AI 处理</p>
        <p className="mt-1 text-xs text-muted">会员功能 · 按顺序逐步执行 · 结果自动刷新</p>
      </div>
      {note && <ErrorNote code={note.code} message={note.message} />}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {AI_BUTTONS.map((b) => (
          <div key={b.kind} className="space-y-1">
            <button
              className="btn btn-secondary w-full"
              disabled={trigger.isPending}
              onClick={() => trigger.mutate(b.kind)}
            >
              {b.label}
            </button>
            <p className="text-center font-mono text-eyebrow text-muted">{b.hint}</p>
          </div>
        ))}
      </div>

      {outputs.data?.transcript && (
        <div className="section-divider">
          <p className="label mb-2">转写 · {outputs.data.transcript.language}</p>
          <p className="max-h-40 overflow-y-auto whitespace-pre-wrap text-sm leading-relaxed">
            {outputs.data.transcript.text}
          </p>
        </div>
      )}

      {outputs.data?.summary && (
        <div className="section-divider space-y-3">
          <p className="label">AI 总结</p>
          <p className="text-sm leading-relaxed">{outputs.data.summary.summary}</p>
          {outputs.data.summary.key_points.length > 0 && (
            <ul className="list-disc space-y-1 pl-5 text-sm">
              {outputs.data.summary.key_points.map((k) => (
                <li key={k}>{k}</li>
              ))}
            </ul>
          )}
          {outputs.data.summary.keywords.length > 0 && (
            <p className="font-mono text-xs text-muted">
              {outputs.data.summary.keywords.join(" · ")}
            </p>
          )}
        </div>
      )}

      {outputs.data?.mindmap && (
        <div className="section-divider">
          <p className="label mb-2">思维导图</p>
          <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md bg-faint p-4 font-mono text-xs leading-relaxed">
            {outputs.data.mindmap.markdown}
          </pre>
          <p className="mt-2 text-xs text-muted">Markdown 大纲（Markmap 渲染接入中）</p>
        </div>
      )}
    </div>
  );
}
