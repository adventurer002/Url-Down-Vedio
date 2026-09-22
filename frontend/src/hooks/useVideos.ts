import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  cancelTask,
  createDownload,
  fetchMe,
  getTask,
  getVideo,
  getVideos,
  parseVideo,
} from "../api/videos";
import type { TaskStatus } from "../types";

const TERMINAL: TaskStatus[] = ["completed", "failed", "cancelled"];

export function isTerminal(status: TaskStatus): boolean {
  return TERMINAL.includes(status);
}

export function useParseVideo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (url: string) => parseVideo(url),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["videos"] });
    },
  });
}

export function useVideo(videoId: string | null) {
  return useQuery({
    queryKey: ["video", videoId],
    queryFn: () => getVideo(videoId as string),
    enabled: !!videoId,
    refetchInterval: (query) =>
      query.state.data && query.state.data.status === "parsing" ? 1500 : false,
  });
}

export function useVideoHistory(page = 1) {
  return useQuery({
    queryKey: ["videos", page],
    queryFn: () => getVideos(page),
  });
}

export function useCreateDownload() {
  return useMutation({
    mutationFn: ({ videoId, formatId }: { videoId: string; formatId: string }) =>
      createDownload(videoId, formatId),
  });
}

export function useTask(taskId: string | null) {
  return useQuery({
    queryKey: ["task", taskId],
    queryFn: () => getTask(taskId as string),
    enabled: !!taskId,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 1000;
      return isTerminal(data.status) ? false : 1000;
    },
  });
}

export function useCancelTask() {
  return useMutation({ mutationFn: (taskId: string) => cancelTask(taskId) });
}

export function useMe(enabled: boolean) {
  return useQuery({
    queryKey: ["me"],
    queryFn: fetchMe,
    enabled,
    retry: false,
  });
}
