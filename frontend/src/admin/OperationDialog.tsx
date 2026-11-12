import { Link } from "react-router-dom";
import { auditLink } from "./AuditPage";
import { bytes } from "./ui";
import { useState, type ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { useAuth, authStore } from "../stores/auth";
import { formatDateTime } from "./ui";
import {
  useOperation,
  type OperationKind,
  type OperationRecord,
} from "./operations";
import type {
  CancelResult,
  CleanupReceipt,
  GrantResult,
  StatusResult,
} from "./api";
import Icon from "../components/Icon";
export function useWriteAccess() {
  const auth = useAuth();
  return Boolean(
    auth.token && auth.user?.role === "admin" && auth.user.status === "active",
  );
}
export function WriteLoginNotice() {
  return (
    <p className="admin-footnote">
      此登录会话仅支持查看。
      <button className="admin-text-button" onClick={authStore.open}>
        重新登录以启用管理操作
      </button>
    </p>
  );
}
export function OperationDialog({
  kind,
  target,
  title,
  trigger,
  children,
  onOpenChange,
}: {
  kind: OperationKind;
  target: string;
  title: string;
  trigger: string;
  children: ReactNode;
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(value) => {
        setOpen(value);
        onOpenChange?.(value);
      }}
    >
      <Dialog.Trigger className="admin-button">{trigger}</Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="admin-overlay" />
        <div className="admin-modal-viewport">
          <Dialog.Content className="admin-dialog admin-operation-dialog">
            <Dialog.Title>{title}</Dialog.Title>
            <Dialog.Description>
              核对目标与影响后提交，操作原因将记录在审计中。
            </Dialog.Description>
            <Dialog.Close
              className="admin-icon-button admin-dialog-close"
              aria-label="关闭操作窗口"
            >
              <Icon name="close" />
            </Dialog.Close>
            <div className="admin-operation-target">
              目标编号：<span>{target}</span>
            </div>
            {children}
          </Dialog.Content>
        </div>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
function Receipt({ record }: { record: OperationRecord }) {
  const result = record.result!;
  let message = "";
  if (record.kind === "status") {
    const r = result as StatusResult;
    message = `操作记录：${r.status === "disabled" ? "停用" : "恢复"}已确认。账号当前状态以刷新后的详情为准。`;
  } else if (record.kind === "grant")
    message = "人工权益发放已确认，不表示真实支付到账。";
  else if (record.kind === "cleanup")
    message = `清理已完成，已释放 ${bytes((result as CleanupReceipt).freed_bytes)}。`;
  else {
    const r = result as CancelResult;
    message =
      r.status === "cancelled"
        ? "服务器已确认任务取消。"
        : r.status === "completed"
          ? "任务已完成，未取消，也未回退已完成结果。"
          : ["running", "processing", "queued"].includes(r.status) &&
              r.cancel_requested
            ? "已请求取消，等待 Worker 确认终态；执行占用暂未提前释放。"
            : "任务状态已变化，本次请求未改变已结束任务。";
  }
  return (
    <div className="admin-operation-receipt" role="status">
      <strong>{message}</strong>
      {result.duplicate && <p>原请求结果重放，没有重复执行。</p>}
      {record.kind === "grant" && (
        <>
          <p>生效：{formatDateTime((result as GrantResult).starts_at)}</p>
          <p>到期：{formatDateTime((result as GrantResult).expires_at)}</p>
          {(result as GrantResult).dispatch_pending && (
            <p>权益已发放，排队任务唤醒待恢复；请勿重复发放。</p>
          )}
        </>
      )}
      {record.kind === "cancel" &&
        (result as CancelResult).notification_pending && (
          <p>取消意图已保存，通知待恢复；不需要重新创建取消操作。</p>
        )}
      {record.kind === "cleanup" && (
        <p>
          处理文件{" "}
          {(result as CleanupReceipt).files! +
            (result as CleanupReceipt).orphan_files!}{" "}
          个 · 临时目录 {(result as CleanupReceipt).temporary_directories} 个
        </p>
      )}
      <p className="admin-wrap">审计编号：{result.audit_id}</p>
    </div>
  );
}
export function OperationState({
  kind,
  target,
  newActionLabel = "重新核对",
}: {
  kind: OperationKind;
  target: string;
  newActionLabel?: string;
}) {
  const operation = useOperation(kind, target);
  const record = operation.record;
  if (!record) return null;
  return (
    <div className="admin-operation-state">
      <dl className="admin-details">
        <dt>操作原因</dt>
        <dd>{record.body.reason}</dd>
        <dt>操作编号</dt>
        <dd>{record.body.key}</dd>
        {kind === "status" && (
          <>
            <dt>提交状态</dt>
            <dd>
              {(record.body as import("./api").StatusRequest).status ===
              "disabled"
                ? "停用账号"
                : "恢复账号"}
            </dd>
            <dt>核对版本</dt>
            <dd>
              {(record.body as import("./api").StatusRequest).expected_version}
            </dd>
          </>
        )}
        {kind === "grant" && (
          <>
            <dt>提交产品</dt>
            <dd>
              {
                {
                  monthly: "旧版月会员",
                  plus_monthly: "Plus 月会员",
                  pro_monthly: "Pro 月会员",
                  asr_120: "120 分钟转写包",
                  ai_100: "100 次 AI 包",
                }[(record.body as import("./api").GrantRequest).product]
              }
            </dd>
          </>
        )}
      </dl>
      {record.phase === "pending" ? (
        <div className="admin-operation-feedback" aria-busy="true">
          <p role="status" className="admin-operation-pending">
            <span className="admin-spinner" aria-hidden="true" />
            正在提交，请勿重复操作。可以关闭窗口，操作编号会保留。
          </p>
        </div>
      ) : record.phase === "confirmed" && record.result ? (
        <Receipt record={record} />
      ) : (
        <div className="admin-operation-feedback" role="alert">
          <strong>
            {record.phase === "uncertain"
              ? "结果待确认"
              : record.kind === "cleanup" &&
                  (record.result as CleanupReceipt | undefined)?.state ===
                    "failed_or_partial"
                ? "清理失败或部分完成"
                : "请求被拒绝"}
          </strong>
          <p>
            {record.error?.message ||
              "上次提交尚未获得确认，请保留原操作编号。"}
          </p>
          {kind === "cleanup" && record.result?.audit_id && (
            <p className="admin-wrap">审计编号：{record.result.audit_id}</p>
          )}
          {record.error?.request_id && (
            <p className="admin-wrap">请求编号：{record.error.request_id}</p>
          )}
          {record.phase === "uncertain" && (
            <p>
              请求内容已锁定。结果不明时请使用原请求核对或查看审计，不要另建重复操作。
            </p>
          )}
          {record.error?.code === "USER_STATUS_CONFLICT" && (
            <p>账号状态或版本已变化。请重新核对最新信息，再确认操作。</p>
          )}
        </div>
      )}
      {record.phase === "uncertain" ? (
        <button
          className="admin-button admin-button-primary"
          onClick={() => void operation.run()}
        >
          {kind === "cleanup" ? "核对原清理结果" : "使用原请求重试"}
        </button>
      ) : (
        record.phase !== "pending" && (
          <button className="admin-button" onClick={operation.reset}>
            {record.phase === "confirmed" ? newActionLabel : "重新核对"}
          </button>
        )
      )}
      <Link
        className="admin-text-button admin-operation-audit-link"
        to={auditLink(kind, target)}
      >
        查看相关审计 →
      </Link>
    </div>
  );
}
export function ReasonFields({
  reason,
  onReason,
  confirmed,
  onConfirmed,
}: {
  reason: string;
  onReason: (value: string) => void;
  confirmed: boolean;
  onConfirmed: (value: boolean) => void;
}) {
  return (
    <>
      <label className="admin-reason">
        操作原因
        <input
          value={reason}
          maxLength={500}
          onChange={(event) => onReason(event.target.value)}
          placeholder="请填写具体业务原因"
          required
        />
      </label>
      <label className="admin-confirm-check">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(event) => onConfirmed(event.target.checked)}
        />
        我已核对目标、操作内容与影响
      </label>
    </>
  );
}
