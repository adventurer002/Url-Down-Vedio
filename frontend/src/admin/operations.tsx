import {
  createContext,
  useContext,
  useEffect,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { authStore, useAuth } from "../stores/auth";
import { API_BASE_URL, ApiError, normalizeError } from "../api/client";
import {
  adminApi,
  type CancelResult,
  type CleanupReceipt,
  type GrantPreview,
  type GrantRequest,
  type GrantResult,
  type OperationRequest,
  type StatusRequest,
  type StatusResult,
} from "./api";
export type OperationKind = "status" | "grant" | "cancel" | "cleanup";
export type OperationBody = StatusRequest | GrantRequest | OperationRequest;
export type OperationResult =
  | StatusResult
  | GrantResult
  | CancelResult
  | CleanupReceipt;
export interface OperationRecord {
  kind: OperationKind;
  target: string;
  body: OperationBody;
  phase: "pending" | "uncertain" | "rejected" | "confirmed";
  preview?: GrantPreview;
  result?: OperationResult;
  error?: { code: string; message: string; request_id: string };
}
export function adminAuthFailure(error: unknown) {
  const e = normalizeError(error);
  return (
    e.status === 401 ||
    (e.status === 403 &&
      ["FORBIDDEN", "ADMIN_BEARER_REQUIRED"].includes(e.code))
  );
}
export function uncertainFailure(error: unknown) {
  const e = normalizeError(error);
  return (
    !e.status ||
    e.status >= 500 ||
    e.status === 408 ||
    [
      "DATABASE_OUTCOME_UNKNOWN",
      "IDEMPOTENCY_CONFLICT",
      "LEGACY_GRANT_EXISTS",
    ].includes(e.code)
  );
}
export const validReason = (value: string) =>
  Boolean(
    typeof value === "string" &&
      value.trim() &&
      value.trim().length <= 500 &&
      !/[\x00-\x1f\x7f]/.test(value.trim()),
  );
const storagePrefix = "videohub.admin.operations.v1:";
function validRecord(value: unknown): value is OperationRecord {
  if (!value || typeof value !== "object") return false;
  const item = value as OperationRecord;
  if (
    !["status", "grant", "cancel", "cleanup"].includes(item.kind) ||
    typeof item.target !== "string" ||
    !item.body ||
    !validReason(item.body.reason || "") ||
    !/^web_[\da-f-]{36}$/i.test(item.body.key || "") ||
    !["pending", "uncertain", "rejected", "confirmed"].includes(item.phase)
  )
    return false;
  if (item.kind === "status") {
    const body = item.body as StatusRequest;
    return (
      ["active", "disabled"].includes(body.status) &&
      ["active", "disabled"].includes(body.expected_status) &&
      Number.isSafeInteger(body.expected_version) &&
      body.expected_version >= 0
    );
  }
  if (item.kind === "grant")
    return ["monthly", "asr_120", "ai_100"].includes(
      (item.body as GrantRequest).product,
    );
  return true;
}
function confirmedResult(record: OperationRecord, result: OperationResult) {
  if (
    !result ||
    typeof result.audit_id !== "string" ||
    !result.audit_id ||
    (record.kind !== "cleanup" && typeof result.duplicate !== "boolean")
  )
    return false;
  if (record.kind === "status") {
    const r = result as StatusResult;
    return (
      r.user_id === record.target &&
      r.status === (record.body as StatusRequest).status &&
      Number.isSafeInteger(r.status_version) &&
      r.status_version >= (record.body as StatusRequest).expected_version
    );
  }
  if (record.kind === "grant") {
    const r = result as GrantResult;
    return (
      r.key === record.body.key &&
      r.product === (record.body as GrantRequest).product &&
      r.mode === "operator_grant" &&
      r.payment_verified === false &&
      Number.isFinite(Date.parse(r.starts_at)) &&
      Number.isFinite(Date.parse(r.expires_at)) &&
      typeof r.dispatch_pending === "boolean"
    );
  }
  if (record.kind === "cleanup") {
    const r = result as CleanupReceipt;
    return (
      r.state === "completed" &&
      typeof r.operation_id === "string" &&
      Boolean(r.operation_id) &&
      [
        r.files,
        r.temporary_directories,
        r.orphan_files,
        r.freed_bytes,
        r.used_bytes,
      ].every((value) => Number.isSafeInteger(value) && value! >= 0)
    );
  }
  const r = result as CancelResult;
  return (
    r.task_id === record.target &&
    [
      "queued",
      "running",
      "processing",
      "completed",
      "failed",
      "cancelled",
      "expired",
    ].includes(r.status) &&
    typeof r.cancel_requested === "boolean" &&
    typeof r.notification_pending === "boolean"
  );
}
function createOperationStore(
  actorId: string,
  token: string,
  invalidate: () => void,
  deny: () => void,
) {
  const storageKey =
    storagePrefix + encodeURIComponent(API_BASE_URL) + ":" + actorId;
  let records: readonly OperationRecord[] = [];
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
    if (Array.isArray(saved))
      records = saved
        .filter(validRecord)
        .map((item) =>
          item.phase === "pending"
            ? { ...item, phase: "uncertain" as const }
            : item,
        );
  } catch {
    /* No credentials are stored here. */
  }
  const listeners = new Set<() => void>();
  const requests = new Set<AbortController>();
  let disposed = false;
  function replace(record: OperationRecord, requireDurable = false) {
    const next = [
      ...records.filter(
        (item) => item.kind !== record.kind || item.target !== record.target,
      ),
      record,
    ];
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(next));
    } catch {
      if (requireDurable)
        throw new ApiError(
          "OPERATION_STORAGE_UNAVAILABLE",
          "无法保存操作编号，本次尚未提交。请恢复浏览器会话存储后重新核对。",
          "",
          409,
        );
    }
    records = next;
    listeners.forEach((listener) => listener());
  }
  return {
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    snapshot: () => records,
    clear(kind: OperationKind, target: string) {
      const record = records.find(
        (item) => item.kind === kind && item.target === target,
      );
      if (record && ["pending", "uncertain"].includes(record.phase)) return;
      records = records.filter((item) => item !== record);
      try {
        sessionStorage.setItem(storageKey, JSON.stringify(records));
      } catch {
        /* Pending records remain protected. */
      }
      listeners.forEach((listener) => listener());
    },
    async run(
      kind: OperationKind,
      target: string,
      proposal?: Omit<OperationRequest, "key"> &
        Partial<StatusRequest & GrantRequest>,
      preview?: GrantPreview,
    ) {
      const prior = records.find(
        (item) => item.kind === kind && item.target === target,
      );
      if (disposed || (prior && prior.phase !== "uncertain")) return;
      const current = authStore.getState();
      if (
        current.user?.id !== actorId ||
        current.token !== token ||
        !token ||
        current.user?.role !== "admin" ||
        current.user.status !== "active"
      )
        return;
      if (!prior && (!proposal || !validReason(proposal.reason))) return;
      const body: OperationBody = prior?.body || {
        ...proposal!,
        reason: proposal!.reason.trim(),
        key: `web_${crypto.randomUUID()}`,
      };
      const pending: OperationRecord = {
        kind,
        target,
        body,
        phase: "pending",
        preview: prior?.preview || preview,
      };
      try {
        replace(pending, true);
      } catch (error) {
        const e = normalizeError(error);
        replace({
          ...pending,
          phase: "rejected",
          error: { code: e.code, message: e.message, request_id: e.request_id },
        });
        return;
      }
      const controller = new AbortController();
      requests.add(controller);
      try {
        const result =
          kind === "status"
            ? await adminApi.changeStatus(
                target,
                body as StatusRequest,
                controller.signal,
              )
            : kind === "grant"
              ? await adminApi.grant(
                  target,
                  body as GrantRequest,
                  controller.signal,
                )
              : kind === "cleanup"
                ? await adminApi.cleanup(body, controller.signal)
                : await adminApi.cancelTask(target, body, controller.signal);
        if (
          kind === "cleanup" &&
          ["accepted", "outcome_unrecorded", "failed_or_partial"].includes(
            (result as CleanupReceipt).state,
          ) &&
          result.audit_id &&
          (result as CleanupReceipt).operation_id
        ) {
          const partial =
            (result as CleanupReceipt).state === "failed_or_partial";
          replace({
            ...pending,
            phase: partial ? "rejected" : "uncertain",
            result,
            error: {
              code: "CLEANUP_REQUIRES_REVIEW",
              message: partial
                ? "清理失败或部分完成，请核查审计后再决定下一次清理。"
                : "清理结果需要核查，不能把它当作成功，也不要另建重复清理。",
              request_id: "",
            },
          });
          if (!disposed) invalidate();
          return;
        }
        if (!confirmedResult(pending, result))
          throw new ApiError(
            "INVALID_OPERATION_RECEIPT",
            "响应缺少可核验的操作结果，请保留原编号核查。",
          );
        replace({ ...pending, phase: "confirmed", result });
        if (!disposed) invalidate();
      } catch (error) {
        const e = normalizeError(error);
        replace({
          ...pending,
          phase: uncertainFailure(e) ? "uncertain" : "rejected",
          error: { code: e.code, message: e.message, request_id: e.request_id },
        });
        if (!disposed) {
          invalidate();
          if (adminAuthFailure(e)) deny();
        }
      } finally {
        requests.delete(controller);
      }
    },
    dispose() {
      disposed = true;
      requests.forEach((controller) => controller.abort());
    },
    activate() {
      disposed = false;
    },
  };
}
type OperationStore = ReturnType<typeof createOperationStore>;
const OperationContext = createContext<OperationStore | null>(null);
export function OperationProvider({
  children,
  onDenied,
}: {
  children: ReactNode;
  onDenied: () => void;
}) {
  const auth = useAuth();
  const client = useQueryClient();
  const [store] = useState(() =>
    createOperationStore(
      auth.user!.id,
      auth.token,
      () => {
        void client.invalidateQueries({ queryKey: ["admin"] }).catch(() => {});
      },
      onDenied,
    ),
  );
  useEffect(() => {
    store.activate();
    return () => store.dispose();
  }, [store]);
  return (
    <OperationContext.Provider value={store}>
      {children}
    </OperationContext.Provider>
  );
}
export function useOperation(kind: OperationKind, target: string) {
  const store = useContext(OperationContext);
  if (!store) throw new Error("OperationProvider required");
  const records = useSyncExternalStore(
    store.subscribe,
    store.snapshot,
    store.snapshot,
  );
  return {
    record: records.find(
      (item) => item.kind === kind && item.target === target,
    ),
    run: (
      proposal?: Parameters<OperationStore["run"]>[2],
      preview?: GrantPreview,
    ) => store.run(kind, target, proposal, preview),
    reset: () => store.clear(kind, target),
  };
}
export function OperationNotice() {
  const store = useContext(OperationContext)!;
  const records = useSyncExternalStore(
    store.subscribe,
    store.snapshot,
    store.snapshot,
  );
  const pending = records.filter(
    (item) => item.phase === "pending" || item.phase === "uncertain",
  );
  const confirmed = [...records]
    .reverse()
    .find((item) => item.phase === "confirmed" && item.result);
  if (!pending.length && !confirmed) return null;
  if (!pending.length && confirmed)
    return (
      <aside
        className="admin-operation-notice admin-operation-confirmed"
        aria-label="已确认的管理操作"
      >
        <strong>最近一项管理操作已确认，详情刷新失败不会撤销该结果。</strong>
        <span className="admin-wrap">
          审计编号：{confirmed.result!.audit_id}
        </span>
        <Link
          to={`/admin/${confirmed.kind === "cleanup" ? "system" : confirmed.kind === "status" ? "users" : confirmed.kind === "grant" ? "allowances" : "tasks"}${confirmed.kind === "cleanup" ? "" : "/" + confirmed.target}`}
        >
          查看操作记录
        </Link>
      </aside>
    );
  return (
    <aside className="admin-operation-notice" aria-label="待确认的管理操作">
      <strong>有 {pending.length} 项管理操作正在处理或结果待确认</strong>
      {pending.map((item) => (
        <Link
          key={`${item.kind}:${item.target}`}
          to={`/admin/${item.kind === "cleanup" ? "system" : item.kind === "status" ? "users" : item.kind === "grant" ? "allowances" : "tasks"}${item.kind === "cleanup" ? "" : "/" + item.target}`}
        >
          {item.kind === "cleanup"
            ? "存储清理"
            : item.kind === "grant"
              ? "权益发放"
              : item.kind === "status"
                ? "账号状态"
                : "任务取消"}{" "}
          · {item.target.slice(0, 8)} ·{" "}
          {item.phase === "pending" ? "处理中" : "结果待确认"}
        </Link>
      ))}
    </aside>
  );
}
