import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { adminApi, type AdminUser } from "./api";
import { ErrorState, Loading } from "./ui";
import { useOperation, validReason } from "./operations";
import {
  OperationDialog,
  OperationState,
  ReasonFields,
  useWriteAccess,
  WriteLoginNotice,
} from "./OperationDialog";
function StatusForm({ id }: { id: string }) {
  const operation = useOperation("status", id);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => {
    if (operation.record) {
      setConfirmed(false);
      setReason("");
    }
  }, [operation.record?.body.key]);
  const query = useQuery({
    queryKey: ["admin", "user", id],
    queryFn: ({ signal }) => adminApi.user(id, signal),
    staleTime: 0,
    refetchOnMount: "always",
    enabled: !operation.record,
  });
  if (operation.record)
    return (
      <OperationState
        kind="status"
        target={id}
        newActionLabel="开始新的状态操作"
      />
    );
  if (query.isPending) return <Loading label="正在核对最新账号状态…" />;
  if (query.isError)
    return (
      <ErrorState error={query.error} retry={() => void query.refetch()} />
    );
  const user = query.data;
  if (user.role !== "user")
    return <p>管理员账号受保护，不能通过网页停用或恢复。</p>;
  if (!["active", "disabled"].includes(user.status))
    return <p>账号状态暂不支持变更。</p>;
  const status = user.status === "active" ? "disabled" : "active";
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (!confirmed || !validReason(reason) || query.isFetching) return;
        void operation.run({
          reason,
          status,
          expected_status: user.status as "active" | "disabled",
          expected_version: user.status_version,
        });
      }}
    >
      {query.isFetching && <Loading label="正在重新核对账号…" compact />}
      <p className="admin-operation-summary">
        {user.nickname} · 当前{user.status === "active" ? "正常" : "停用"} · 将
        {status === "disabled" ? "停用" : "恢复"}
      </p>
      <p className="admin-footnote">
        {status === "disabled"
          ? "停用会阻止后续登录和新的任务准入，不强行打断已运行任务，也不清空已有权益。"
          : "恢复后用户需要重新登录，旧登录凭证不会恢复有效。"}
      </p>
      <ReasonFields
        reason={reason}
        onReason={setReason}
        confirmed={confirmed}
        onConfirmed={setConfirmed}
      />
      <button
        className="admin-button admin-button-primary"
        disabled={!confirmed || !validReason(reason) || query.isFetching}
      >
        {status === "disabled" ? "确认停用" : "确认恢复"}
      </button>
    </form>
  );
}
export default function StatusAction({ user }: { user: AdminUser }) {
  const access = useWriteAccess();
  const { record } = useOperation("status", user.id);
  if (!access) return <WriteLoginNotice />;
  if (user.role !== "user" && !record)
    return (
      <p className="admin-footnote">管理员账号受保护，不提供网页停用或恢复。</p>
    );
  return (
    <OperationDialog
      kind="status"
      target={user.id}
      title="账号状态操作"
      trigger={
        record
          ? "查看状态操作"
          : user.status === "active"
            ? "停用账号"
            : "恢复账号"
      }
    >
      <StatusForm id={user.id} />
    </OperationDialog>
  );
}
