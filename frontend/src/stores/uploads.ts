import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import { authStore } from "./auth";
import { terminal } from "../lib/taskTracker";
import type {
  UploadRecord,
  UploadTransfer,
  RequestIdentity,
} from "../types/uploads";
import type { TaskCreated } from "../types/api";
import type { LocalTask } from "./workspace";

export const isSending = (entry: UploadTransfer) =>
  ["sending", "confirming"].includes(entry.phase);
export function validateFile(file: File, maxBytes?: number) {
  if (!/\.(mp4|mov|mkv|webm|mp3|m4a)$/i.test(file.name))
    throw new ApiError(
      "UPLOAD_FORMAT_UNSUPPORTED",
      "请选择 MP4、MOV、MKV、WebM 视频或 MP3、M4A 音频。",
    );
  if (!file.size)
    throw new ApiError("UPLOAD_SIZE_INVALID", "文件内容为空，请重新选择。");
  if (maxBytes && file.size > maxBytes)
    throw new ApiError(
      "FILE_TOO_LARGE",
      `当前账号单个文件最多 ${(maxBytes / 1024 ** 2).toFixed(0)} MB。`,
    );
}
interface Hooks {
  notify: () => void;
  record: (value: UploadRecord) => string;
  probe: (value: UploadRecord, task: TaskCreated) => void;
  tasks: () => LocalTask[];
  cancelTask: (task: LocalTask) => Promise<void>;
  snapshot: (
    task: LocalTask,
    value: Awaited<ReturnType<typeof api.task>>,
  ) => void;
}
export function createUploadManager(hooks: Hooks) {
  let userId = "",
    revision = 0,
    starting = false;
  let entries: UploadTransfer[] = [];
  const files = new Map<string, File>();
  const previews = new Map<string, string>();
  const controllers = new Map<string, AbortController>();
  const reads = new Map<string, Promise<void>>();
  const resuming = new Set<string>();
  function persist() {
    if (userId)
      try {
        localStorage.setItem(
          `videohub.uploads.v1:${userId}`,
          JSON.stringify(entries),
        );
      } catch {
        /* metadata only */
      }
    hooks.notify();
  }
  function identity(): RequestIdentity {
    const value = authStore.getState();
    if (!value.user || value.user.id !== userId)
      throw new ApiError("AUTH_REQUIRED", "请先登录后上传文件。");
    return { userId, token: value.token };
  }
  function current(expected: number) {
    return revision === expected && authStore.getState().user?.id === userId;
  }
  function revoke(id: string) {
    const preview = previews.get(id);
    if (preview) URL.revokeObjectURL(preview);
    previews.delete(id);
    files.delete(id);
  }
  function apply(entry: UploadTransfer, record: UploadRecord) {
    entry.record = record;
    entry.key = hooks.record(record);
    if (record.status === "ready") entry.phase = "ready";
    else if (["expired", "deleted"].includes(record.status)) {
      entry.phase = "cancelled";
      revoke(record.id);
    } else if (record.status === "failed") {
      entry.phase = "failed";
      entry.errorCode = record.error_code || "UPLOAD_PROBE_FAILED";
      entry.error = "文件校验未完成，请查看原因后重新上传。";
    } else if (record.status === "validating") entry.phase = "validating";
    persist();
  }
  async function finish(
    entry: UploadTransfer,
    expected: number,
    controller: AbortController,
  ) {
    if (!current(expected)) return;
    const task = await api.completeUpload(
      entry.id,
      controller.signal,
      identity(),
    );
    if (!current(expected) || controller.signal.aborted) return;
    entry.record = {
      ...entry.record,
      task_id: task.task_id,
      status: "validating",
    };
    entry.phase = "validating";
    entry.error = entry.errorCode = undefined;
    hooks.record(entry.record);
    hooks.probe(entry.record, task);
    persist();
  }
  async function recover(id: string, complete = true): Promise<void> {
    if (reads.has(id)) return reads.get(id);
    const entry = entries.find((e) => e.id === id);
    if (!entry || !userId || isSending(entry) || entry.phase === "cancelling")
      return;
    const expected = revision,
      controller = new AbortController();
    controllers.set(`read:${id}`, controller);
    const operation = (async () => {
      try {
        const record = await api.upload(id, controller.signal);
        if (!current(expected) || controller.signal.aborted) return;
        apply(entry, record);
        if (record.status === "uploaded") {
          entry.phase = "interrupted";
          entry.error = "文件已经上传，可以继续校验。";
          if (complete) await finish(entry, expected, controller);
        } else if (record.status === "pending") {
          entry.phase = "interrupted";
          entry.error = "上传尚未完成，请重新选择原文件后继续。";
        } else if (record.status === "validating" && record.task_id) {
          hooks.probe(record, { task_id: record.task_id, status: "queued" });
        }
        persist();
      } catch (cause) {
        if (current(expected) && !controller.signal.aborted) {
          const error = normalizeError(cause);
          entry.error = error.message;
          entry.errorCode = error.code;
          if (error.status === 404 || error.status === 410)
            entry.phase = "failed";
          persist();
        }
      } finally {
        if (current(expected)) {
          reads.delete(id);
          controllers.delete(`read:${id}`);
        }
      }
    })();
    reads.set(id, operation);
    return operation;
  }
  async function send(entry: UploadTransfer, file: File) {
    const expected = revision,
      controller = new AbortController(),
      who = identity();
    controllers.set(entry.id, controller);
    entry.phase = "sending";
    entry.loaded = 0;
    entry.error = entry.errorCode = undefined;
    files.set(entry.id, file);
    persist();
    try {
      const record = await api.uploadContent(
        entry.id,
        file,
        controller.signal,
        (loaded) => {
          if (!current(expected) || controller.signal.aborted) return;
          entry.loaded = Math.min(loaded, file.size);
          if (entry.loaded >= file.size) entry.phase = "confirming";
          hooks.notify();
        },
        who,
      );
      if (!current(expected) || controller.signal.aborted) return;
      entry.loaded = file.size;
      apply(entry, record);
      await finish(entry, expected, controller);
    } catch (cause) {
      if (!current(expected) || controller.signal.aborted) return;
      const error = normalizeError(cause);
      entry.phase = "interrupted";
      entry.error = error.message;
      entry.errorCode = error.code;
      persist();
      // An acknowledgement may be lost after the bytes or probe were committed.
      await recover(entry.id, false);
    } finally {
      if (current(expected)) controllers.delete(entry.id);
    }
  }
  async function begin(file: File, maxBytes?: number) {
    validateFile(file, maxBytes);
    if (
      starting ||
      entries.some((e) => isSending(e) || e.phase === "cancelling")
    )
      throw new ApiError(
        "UPLOAD_ALREADY_RUNNING",
        "有文件正在上传，请先完成或取消。",
      );
    const expected = revision,
      who = identity(),
      controller = new AbortController();
    starting = true;
    controllers.set("create", controller);
    try {
      const record = await api.createUpload(
        file.name,
        file.size,
        controller.signal,
        who,
      );
      if (!current(expected))
        throw new ApiError(
          "SESSION_CHANGED",
          "登录身份已变化，请重新选择文件。",
        );
      const entry: UploadTransfer = {
        id: record.id,
        key: hooks.record(record),
        record,
        phase: "sending",
        loaded: 0,
      };
      entries = [entry, ...entries].slice(0, 30);
      persist();
      void send(entry, file);
      return entry.key;
    } finally {
      if (current(expected)) {
        starting = false;
        controllers.delete("create");
      }
    }
  }
  async function resume(id: string, file?: File) {
    const entry = entries.find((e) => e.id === id);
    if (
      !entry ||
      resuming.has(id) ||
      isSending(entry) ||
      entry.phase === "cancelling"
    )
      return;
    resuming.add(id);
    try {
      await recover(id, false);
      if (!entries.includes(entry) || entry.record.status !== "pending") {
        if (entry.record.status === "uploaded") await recover(id);
        return;
      }
      const selected = file || files.get(id);
      if (!selected)
        throw new ApiError(
          "UPLOAD_RESELECT_REQUIRED",
          "请选择原文件以重新上传。",
        );
      validateFile(selected);
      if (
        selected.name !== entry.record.filename ||
        selected.size !== entry.record.expected_bytes
      )
        throw new ApiError(
          "UPLOAD_FILE_CHANGED",
          "请选择名称和大小相同的原文件，或返回导入页创建新上传。",
        );
      await send(entry, selected);
    } finally {
      resuming.delete(id);
    }
  }
  async function remove(id: string, cancelTasks: boolean) {
    const entry = entries.find((e) => e.id === id),
      expected = revision;
    if (!entry)
      throw new ApiError("UPLOAD_NOT_FOUND", "请先恢复文件状态后重试。");
    const previous = entry.phase;
    entry.phase = "cancelling";
    controllers.get(id)?.abort();
    controllers.get(`read:${id}`)?.abort();
    persist();
    const deadline = Date.now() + 20000;
    try {
      const record = await api.upload(id);
      if (!current(expected)) return;
      const related = hooks
        .tasks()
        .filter(
          (task) =>
            !terminal(task.event.status) &&
            (task.videoKey === entry.key || task.id === record.task_id),
        );
      if (
        record.task_id &&
        !related.some((task) => task.id === record.task_id)
      ) {
        const snapshot = await api.task(record.task_id);
        if (!current(expected)) return;
        if (!terminal(snapshot.status)) {
          hooks.probe(record, {
            task_id: snapshot.task_id,
            status: snapshot.status,
          });
          const task = hooks.tasks().find((t) => t.id === snapshot.task_id);
          if (task) related.push(task);
        }
      }
      if (related.length && !cancelTasks)
        throw new ApiError(
          "UPLOAD_IN_USE",
          "文件仍有任务正在处理，请确认取消处理后再删除。",
          "",
          409,
        );
      for (const task of related) {
        if (!current(expected)) return;
        await hooks.cancelTask(task);
      }
      for (const task of related) {
        while (current(expected) && !terminal(task.event.status)) {
          if (Date.now() >= deadline)
            throw new ApiError(
              "CANCEL_PENDING",
              "取消尚未确认，请稍后再尝试删除原文件。",
            );
          const snapshot = await api.task(task.id);
          if (!current(expected)) return;
          hooks.snapshot(task, snapshot);
          if (!terminal(snapshot.status))
            await new Promise((resolve) => setTimeout(resolve, 500));
        }
      }
      while (current(expected)) {
        try {
          const deleted = await api.deleteUpload(id);
          if (current(expected)) apply(entry, deleted);
          return;
        } catch (cause) {
          const error = normalizeError(cause);
          if (error.code !== "UPLOAD_IN_USE" || Date.now() >= deadline)
            throw error;
          await new Promise((resolve) => setTimeout(resolve, 250));
        }
      }
    } catch (cause) {
      if (!current(expected)) return;
      entry.phase = isSending({ ...entry, phase: previous })
        ? "interrupted"
        : previous;
      const error = normalizeError(cause);
      entry.error = error.message;
      entry.errorCode = error.code;
      persist();
      throw error;
    }
  }
  return {
    get entries() {
      return entries;
    },
    begin,
    resume,
    recover,
    remove,
    async refresh(id: string) {
      await reads.get(id);
      await recover(id);
    },
    adopt(record: UploadRecord) {
      let entry = entries.find((e) => e.id === record.id);
      if (!entry) {
        entry = {
          id: record.id,
          record,
          key: "",
          phase: "interrupted",
          loaded: record.size_bytes,
        };
        entries.push(entry);
      }
      apply(entry, record);
    },
    preview(id: string) {
      if (
        !previews.has(id) &&
        files.has(id) &&
        typeof URL.createObjectURL === "function"
      )
        previews.set(id, URL.createObjectURL(files.get(id)!));
      return previews.get(id);
    },
    hasFile: (id: string) => files.has(id),
    restore() {
      for (const entry of entries)
        if (!["cancelled", "failed"].includes(entry.phase))
          void recover(entry.id);
    },
    switchUser(next: string) {
      if (next === userId) return;
      revision++;
      starting = false;
      controllers.forEach((c) => c.abort());
      controllers.clear();
      reads.clear();
      resuming.clear();
      [...files.keys()].forEach(revoke);
      userId = next;
      entries = [];
      if (next)
        try {
          const cached = JSON.parse(
            localStorage.getItem(`videohub.uploads.v1:${next}`) || "[]",
          ) as UploadTransfer[];
          entries = Array.isArray(cached)
            ? cached
                .filter((e) => e?.id && e.record?.id === e.id)
                .slice(0, 30)
                .map((e) => ({
                  ...e,
                  phase:
                    isSending(e) || e.phase === "cancelling"
                      ? ("interrupted" as const)
                      : e.phase,
                }))
            : [];
        } catch {
          /* corrupted metadata does not prevent a fresh import */
        }
      hooks.notify();
    },
  };
}
