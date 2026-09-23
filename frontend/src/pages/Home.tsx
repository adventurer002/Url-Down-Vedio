import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { downloadFileUrl } from "../api/videos";
import { ApiRequestError } from "../api/client";
import { ErrorNote, UrlForm, VideoCard } from "../components/VideoCard";
import { AiPanel } from "../components/AiPanel";
import TextLoop from "../components/TextLoop";
import { TaskStatusLine } from "../components/VideoCard";
import { useAuthStore } from "../stores/auth";
import {
  isTerminal,
  useCancelTask,
  useCreateDownload,
  useParseVideo,
  useTask,
  useVideo,
} from "../hooks/useVideos";
import { useTaskEvents, type SseEvent } from "../hooks/useTaskEvents";

export function HomePage() {
  const loggedIn = useAuthStore((s) => !!s.accessToken);
  const [videoId, setVideoId] = useState<string | null>(null);
  const [formatId, setFormatId] = useState("");
  const [taskId, setTaskId] = useState<string | null>(null);
  const [sseState, setSseState] = useState<SseEvent | null>(null);
  const [note, setNote] = useState<{ code: string; message: string } | null>(null);
  const [searchParams] = useSearchParams();

  useEffect(() => {
    const vid = searchParams.get("video");
    if (vid && !videoId) setVideoId(vid);
  }, [searchParams, videoId]);

  const parse = useParseVideo();
  const videoQuery = useVideo(videoId);
  const createDownload = useCreateDownload();
  const cancelTask = useCancelTask();
  const taskQuery = useTask(taskId);

  const video = videoQuery.data ?? null;
  const task = taskQuery.data ?? null;

  const onSse = useCallback((e: SseEvent) => {
    setSseState(e);
  }, []);
  useTaskEvents(!task || isTerminal(task.status) ? null : taskId, onSse);

  const submitUrl = (url: string) => {
    setNote(null);
    setVideoId(null);
    setFormatId("");
    setTaskId(null);
    setSseState(null);
    parse.mutate(url, {
      onSuccess: (data) => setVideoId(data.video_id),
      onError: (e) => {
        if (e instanceof ApiRequestError) setNote({ code: e.code, message: e.message });
      },
    });
  };

  const startDownload = () => {
    if (!video || !formatId) return;
    setNote(null);
    createDownload.mutate(
      { videoId: video.id, formatId },
      {
        onSuccess: (data) => {
          setTaskId(data.task_id);
          setSseState(null);
        },
        onError: (e) => {
          if (e instanceof ApiRequestError) setNote({ code: e.code, message: e.message });
        },
      },
    );
  };

  const progress = sseState?.end ? 100 : (sseState?.progress ?? task?.progress ?? 0);
  const status = sseState?.status ?? task?.status;
  const done = status === "completed";

  return (
    <div className="flex min-h-[calc(100vh-160px)] flex-col items-center justify-center gap-8 py-10">
      <section className="text-center">
        <p className="eyebrow">Video · Download · AI</p>
        <h1 className="mx-auto mt-4 max-w-2xl text-4xl font-normal leading-[1.15] tracking-[-0.02em] sm:text-heading-lg">
          粘贴链接，
          <br />
          剩下的交给我们。
        </h1>
        <p className="mx-auto mt-4 max-w-xl text-base text-muted">
          解析视频信息，一键下载，会员解锁转写、总结与思维导图。
        </p>
      </section>

      <TextLoop
        text={["TikTok", "YouTube", "Bilibili", "Instagram", "抖音", "Vimeo", "Twitter", "Dailymotion"]}
        shape="line"
        speed={80}
        direction="forward"
        separator="✦"
        curviness={0}
        fontSize={40}
        fontWeight={500}
        letterSpacing={10}
        uppercase
        color="#171717"
        ribbon={false}
        pauseOnHover
        className="h-[45px] flex items-center "
      />

      <section className="mx-auto max-w-2xl">
        <UrlForm onSubmit={submitUrl} loading={parse.isPending} />
        {note && (
          <div className="mt-3">
            <ErrorNote code={note.code} message={note.message} />
          </div>
        )}
        {!loggedIn && (
          <p className="mt-3 text-center text-sm text-muted">
            游客可解析。下载需要
            <Link to="/login" className="mx-1 underline underline-offset-4 hover:text-ink">
              登录
            </Link>
            。
          </p>
        )}
      </section>

      {video && (
        <section className="mx-auto max-w-2xl space-y-4">
          {video.status === "parsing" && (
            <div className="card p-5 text-sm text-muted">正在解析视频信息…</div>
          )}
          {video.status === "failed" && (
            <ErrorNote code="parse_failed" message={video.error_message ?? "解析失败"} />
          )}
          {video.status === "ready" && (
            <>
              <VideoCard video={video} selectedFormat={formatId} onSelectFormat={setFormatId} />
              {loggedIn && <AiPanel videoId={video.id} />}
              <div className="flex items-center gap-3">
                <button
                  className="btn btn-primary flex-1"
                  disabled={!formatId || createDownload.isPending || !!taskId}
                  onClick={startDownload}
                >
                  {createDownload.isPending ? "创建中…" : taskId ? "已开始下载" : "下载"}
                </button>
                {taskId && status && !isTerminal(status) && (
                  <button
                    className="btn btn-secondary"
                    onClick={() => cancelTask.mutate(taskId)}
                    disabled={cancelTask.isPending}
                  >
                    取消
                  </button>
                )}
              </div>
            </>
          )}
        </section>
      )}

      {task && status && (
        <section className="mx-auto max-w-2xl">
          <div className="card space-y-3 p-5">
            <TaskStatusLine task={{ ...task, status, progress }} />
            {done && (
              <a className="btn btn-primary w-full" href={downloadFileUrl(task.id)}>
                保存文件
              </a>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
