import CancelAction from "./CancelAction";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useParams } from "react-router-dom";
import { adminApi } from "./api";
import {
  ErrorState,
  formatDateTime,
  Loading,
  Panel,
  StatusBadge,
  taskLabels,
} from "./ui";
import { isUuid, PageHeading } from "./list";

export const activeTask = (status: string) =>
  ["queued", "running", "processing"].includes(status);
export const waitLabels: Record<string, string> = {
  allowance: "等待额度",
  capacity: "等待执行容量",
  entitlement: "等待权益",
  subscription: "等待关注公众号",
  daily_downloads: "等待每日下载额度",
  quote_required: "缺少有效额度确认",
};
export function TaskDetail({ id }: { id: string }) {
  const query = useQuery({
    queryKey: ["admin", "task", id],
    queryFn: ({ signal }) => adminApi.task(id, signal),
    refetchInterval: (query) =>
      activeTask(query.state.data?.status || "") ? 5000 : false,
  });
  if (query.isPending) return <Loading />;
  if (query.isError)
    return (
      <ErrorState error={query.error} retry={() => void query.refetch()} />
    );
  const task = query.data;
  return (
    <>
      <section className="admin-task-state" aria-label="任务执行概况">
        <div>
          <strong>
            {task.video?.title || taskLabels[task.task_type] || "任务"}
          </strong>
          <span>
            {activeTask(task.status) ? `当前进度 ${task.progress}%` : "已结束"}
          </span>
        </div>
        <progress
          className="admin-task-progress"
          aria-label="任务执行进度"
          max={100}
          value={task.progress}
        />
        <p>
          {task.cancel_requested && activeTask(task.status)
            ? "取消请求已保存，等待执行端确认退出。"
            : task.error_code
              ? `执行遇到问题：${task.error_code}`
              : task.status === "queued"
                ? `正在排队${task.wait_reason ? `：${waitLabels[task.wait_reason] || task.wait_reason}` : "，等待执行"}。`
                : task.status === "completed"
                  ? "任务已完成，可在下方核对执行与结算记录。"
                  : activeTask(task.status)
                    ? "执行状态每 5 秒自动核对。"
                    : "终态记录已保留，可在下方查看时间与额度结算。"}
        </p>
      </section>
      <div className="admin-detail-actions">
        <CancelAction task={task} />
      </div>
      <dl className="admin-details">
        <dt>任务编号</dt>
        <dd>{task.task_id}</dd>
        <dt>视频</dt>
        <dd>{task.video?.title || "无视频信息"}</dd>
        <dt>任务类型</dt>
        <dd>{taskLabels[task.task_type] || task.task_type}</dd>
        <dt>状态</dt>
        <dd>
          <StatusBadge status={task.status} />{" "}
          {task.cancel_requested && "已请求取消"}
        </dd>
        <dt>进度</dt>
        <dd>{task.progress}%</dd>
        <dt>等待原因</dt>
        <dd>
          {task.wait_reason
            ? waitLabels[task.wait_reason] || task.wait_reason
            : "—"}
        </dd>
        <dt>用户编号</dt>
        <dd>
          {task.user_id ? (
            <Link
              className="admin-text-button admin-wrap"
              to={`/admin/users/${task.user_id}`}
            >
              {task.user_id}
            </Link>
          ) : (
            "匿名任务"
          )}
        </dd>
        <dt>创建时间</dt>
        <dd>{formatDateTime(task.created_at)}</dd>
        <dt>开始时间</dt>
        <dd>{formatDateTime(task.started_at)}</dd>
        <dt>完成时间</dt>
        <dd>{formatDateTime(task.finished_at)}</dd>
        <dt>排队时长</dt>
        <dd>{Math.round(task.queue_seconds)} 秒</dd>
        <dt>执行时长</dt>
        <dd>
          {task.execution_seconds == null
            ? "尚未执行"
            : `${Math.round(task.execution_seconds)} 秒`}
        </dd>
        <dt>执行尝试</dt>
        <dd>{task.attempts ?? "—"}</dd>
        <dt>用户已隐藏</dt>
        <dd>
          {task.history_hidden == null
            ? "—"
            : task.history_hidden
              ? "是"
              : "否"}
        </dd>
        <dt>错误代码</dt>
        <dd>{task.error_code || "—"}</dd>
      </dl>
      <h3 className="admin-detail-subtitle">额度占用与结算</h3>
      {task.allowance ? (
        <dl className="admin-details">
          <dt>占用状态</dt>
          <dd>
            {{ held: "占用中", settled: "已结算", released: "已释放" }[
              task.allowance.state
            ] || task.allowance.state}
          </dd>
          <dt>业务日期</dt>
          <dd>{task.allowance.business_date}</dd>
          <dt>确认上限</dt>
          <dd>
            转写 {task.allowance.maximum.asr_seconds} 秒 · AI{" "}
            {task.allowance.maximum.ai_generations} 次
          </dd>
          <dt>计量转写</dt>
          <dd>{task.allowance.measured_asr_seconds} 秒</dd>
          <dt>最终结算</dt>
          <dd>
            {task.allowance.settlement
              ? `转写 ${task.allowance.settlement.asr_seconds ?? "—"} 秒 · AI ${task.allowance.settlement.ai_generations ?? "—"} 次`
              : "尚无结算记录"}
          </dd>
        </dl>
      ) : (
        <p className="admin-footnote">
          暂无额度占用记录；历史任务或不消耗额度的任务可能没有记录。
        </p>
      )}
    </>
  );
}
export default function TaskDetailPage() {
  const { id = "" } = useParams();
  const { search } = useLocation();
  return (
    <>
      <PageHeading
        title="任务详情"
        description="仅查看任务元数据；时间均为北京时间。"
        action={
          <Link className="admin-button" to={`/admin/tasks${search}`}>
            返回任务列表
          </Link>
        }
      />
      <Panel title="任务信息">
        {isUuid(id) ? (
          <TaskDetail id={id} />
        ) : (
          <p role="alert">任务编号无效，需要完整 UUID。</p>
        )}
      </Panel>
    </>
  );
}
