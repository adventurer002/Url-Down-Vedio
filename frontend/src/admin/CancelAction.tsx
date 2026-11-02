import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { adminApi, type AdminTask } from "./api";
import { ErrorState, Loading, StatusBadge } from "./ui";
import { useOperation, validReason } from "./operations";
import {
  OperationDialog,
  OperationState,
  ReasonFields,
  useWriteAccess,
  WriteLoginNotice,
} from "./OperationDialog";
const cancellable = (status: string) =>
  ["queued", "running", "processing"].includes(status);
function CancelForm({ id }: { id: string }) {
  const operation = useOperation("cancel", id);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => {
    if (operation.record) {
      setConfirmed(false);
      setReason("");
    }
  }, [operation.record?.body.key]);
  const query = useQuery({
    queryKey: ["admin", "task", id],
    queryFn: ({ signal }) => adminApi.task(id, signal),
    enabled: !operation.record,
    staleTime: 0,
    refetchOnMount: "always",
  });
  if (operation.record)
    return (
      <OperationState kind="cancel" target={id} newActionLabel="重新核对任务" />
    );
  if (query.isPending) return <Loading label="正在核对任务状态…" />;
  if (query.isError)
    return (
      <ErrorState error={query.error} retry={() => void query.refetch()} />
    );
  if (!cancellable(query.data.status))
    return <p>任务已进入终态，无需取消。已完成结果不会被回退。</p>;
  if (query.data.cancel_requested)
    return <p>已请求取消，等待服务器确认终态。无需重复提交。</p>;
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (!confirmed || !validReason(reason) || query.isFetching) return;
        void operation.run({ reason });
      }}
    >
      {query.isFetching && <Loading label="正在重新核对任务…" compact />}
      <p className="admin-operation-summary">
        {query.data.video?.title || "任务"} ·{" "}
        <StatusBadge status={query.data.status} />
      </p>
      <p className="admin-footnote">
        排队任务可直接取消；执行中任务需等待 Worker
        确认退出。取消申请不表示任务已结束，也不会提前释放占用。
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
        确认请求取消
      </button>
    </form>
  );
}
export default function CancelAction({ task }: { task: AdminTask }) {
  const access = useWriteAccess();
  const { record } = useOperation("cancel", task.task_id);
  if (!record && (!cancellable(task.status) || task.cancel_requested))
    return (
      <p className="admin-footnote">
        {task.cancel_requested && cancellable(task.status)
          ? "已请求取消，等待确认终态。"
          : "任务已结束，无需取消。"}
      </p>
    );
  if (!access) return <WriteLoginNotice />;
  return (
    <OperationDialog
      kind="cancel"
      target={task.task_id}
      title="请求取消任务"
      trigger={record ? "查看取消操作" : "请求取消任务"}
    >
      <CancelForm id={task.task_id} />
    </OperationDialog>
  );
}
