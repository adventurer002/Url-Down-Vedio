import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi, type AuditEvent, type AuditFilters } from "./api";
import {
  DetailDialog,
  ErrorState,
  formatDateTime,
  DateTimeCell,
  CopyId,
  Loading,
  Panel,
  shortId,
  StatusBadge,
  PageSummary,
} from "./ui";
import {
  EmptyState,
  FilterForm,
  type FilterField,
  isUuid,
  PageHeading,
  pageOffset,
  Pagination,
  readTaskFilters,
  QuickFilters,
} from "./list";
export const actionLabels: Record<string, string> = {
  "admin.bootstrap": "初始化管理员",
  "user.status": "账号状态变更",
  "allowance.grant": "人工权益发放",
  "task.cancel": "请求取消任务",
  "storage.cleanup.request": "提交存储清理",
  "storage.cleanup.finish": "存储清理结果",
};
export const outcomeLabels: Record<string, string> = {
  succeeded: "已成功",
  accepted: "已接受",
  unchanged: "未改变",
  failed: "失败",
  unknown: "结果未知",
};
const targetLabels: Record<string, string> = {
  user: "用户",
  task: "任务",
  storage: "存储",
};
export function auditLink(kind: string, target: string) {
  return `/admin/audit?${new URLSearchParams({ target_type: kind === "cleanup" ? "storage" : kind === "cancel" ? "task" : "user", target_id: target, action: kind === "cleanup" ? "" : kind === "status" ? "user.status" : kind === "grant" ? "allowance.grant" : "task.cancel" })}`;
}
export function readAuditFilters(params: URLSearchParams): {
  filters: AuditFilters;
  error: string;
} {
  const filters: AuditFilters = {
    limit: 20,
    offset: pageOffset(params),
    actor_id: params.get("actor_id")?.trim() || "",
    action: params.get("action") || "",
    target_type: params.get("target_type") || "",
    target_id: params.get("target_id")?.trim() || "",
    outcome: params.get("outcome") || "",
  };
  if (filters.actor_id && !isUuid(filters.actor_id))
    return { filters, error: "操作人编号需要填写完整 UUID。" };
  if (
    (filters.action && !Object.hasOwn(actionLabels, filters.action)) ||
    (filters.outcome && !Object.hasOwn(outcomeLabels, filters.outcome)) ||
    (filters.target_type && !Object.hasOwn(targetLabels, filters.target_type))
  )
    return { filters, error: "审计筛选条件无效，请重新选择。" };
  if ((filters.target_id?.length || 0) > 128)
    return { filters, error: "对象编号最多 128 个字符。" };
  const interval = new URLSearchParams();
  for (const key of ["since", "until"]) {
    const value = params.get(key);
    if (value) interval.set(key, value);
  }
  const dates = readTaskFilters(interval);
  filters.since = dates.filters.since;
  filters.until = dates.filters.until;
  return { filters, error: dates.error };
}
const fields: FilterField[] = [
  {
    name: "actor_id",
    label: "操作人编号",
    placeholder: "完整 UUID",
    advanced: true,
  },
  { name: "action", label: "操作动作", options: actionLabels },
  { name: "target_type", label: "对象类型", options: targetLabels },
  {
    name: "target_id",
    label: "对象编号",
    placeholder: "用户 / 任务 UUID，存储填 local",
    maxLength: 128,
    advanced: true,
  },
  { name: "outcome", label: "操作结果", options: outcomeLabels },
  {
    name: "since",
    label: "开始时间（北京时间）",
    type: "datetime-local",
    advanced: true,
  },
  {
    name: "until",
    label: "结束时间（北京时间）",
    type: "datetime-local",
    advanced: true,
  },
];
function AuditDetail({ event }: { event: AuditEvent }) {
  const target =
    event.target_type === "storage"
      ? "/admin/system"
      : isUuid(event.target_id)
        ? `/admin/${event.target_type === "task" ? "tasks" : "users"}/${event.target_id}`
        : "";
  return (
    <>
      <dl className="admin-details">
        <dt>审计编号</dt>
        <dd>{event.id}</dd>
        <dt>操作人</dt>
        <dd>
          {event.actor_label}
          {event.actor_user_id && `（${event.actor_user_id}）`}
        </dd>
        <dt>动作</dt>
        <dd>{actionLabels[event.action] || event.action}</dd>
        <dt>当时结果</dt>
        <dd>{outcomeLabels[event.outcome] || event.outcome}</dd>
        <dt>操作对象</dt>
        <dd>
          {target ? (
            <Link className="admin-text-button admin-wrap" to={target}>
              {event.target_id}
            </Link>
          ) : (
            event.target_id
          )}
        </dd>
        <dt>操作原因</dt>
        <dd>{event.reason}</dd>
        <dt>发生时间</dt>
        <dd>{formatDateTime(event.created_at)}</dd>
        <dt>请求编号</dt>
        <dd>{event.request_id}</dd>
        <dt>操作编号</dt>
        <dd>{event.idempotency_key}</dd>
        {event.current_task_status && (
          <>
            <dt>任务当前状态</dt>
            <dd>
              <StatusBadge status={event.current_task_status} />
            </dd>
          </>
        )}
      </dl>
      <p className="admin-footnote">
        “已接受”仅表示请求被接受，不代表进程退出。当前任务状态来自本次查询，原审计事件不会被改写。
      </p>
      {[
        ["请求白名单", event.request_data],
        ["必要前后值", event.changes],
        ["结果记录", event.result],
      ].map(([label, value]) => (
        <details className="admin-audit-json" key={String(label)}>
          <summary>{String(label)}</summary>
          <pre>{JSON.stringify(value, null, 2)}</pre>
        </details>
      ))}
    </>
  );
}
export default function AuditPage() {
  const [params, setParams] = useSearchParams();
  const { filters, error } = readAuditFilters(params);
  const query = useQuery({
    queryKey: ["admin", "audit", filters],
    queryFn: ({ signal }) => adminApi.audit(filters, signal),
    enabled: !error,
  });
  return (
    <>
      <PageHeading
        title="操作审计"
        description="追溯管理员操作、请求结果与必要变更记录。"
      />
      <PageSummary
        busy={query.isFetching}
        items={[
          {
            label: "匹配记录",
            value: query.data?.total,
            note: "当前筛选的全部审计",
            icon: "history",
          },
          {
            label: "本页已成功",
            value: query.data?.items.filter(
              (event) => event.outcome === "succeeded",
            ).length,
            note: "请求已产生确认结果",
            icon: "shield",
          },
          {
            label: "本页已接受",
            value: query.data?.items.filter(
              (event) => event.outcome === "accepted",
            ).length,
            note: "不等同于任务已结束",
            icon: "clock",
          },
          {
            label: "本页需核查",
            value: query.data?.items.filter((event) =>
              ["failed", "unknown"].includes(event.outcome),
            ).length,
            note: "失败或结果未知的记录",
            icon: "search",
          },
        ]}
      />
      <QuickFilters
        items={[
          { label: "全部操作结果", values: { outcome: "" } },
          { label: "成功记录", values: { outcome: "succeeded" } },
          { label: "已接受请求", values: { outcome: "accepted" } },
          { label: "失败记录", values: { outcome: "failed" } },
          { label: "结果未知记录", values: { outcome: "unknown" } },
        ]}
      />
      <FilterForm
        key={params.toString()}
        fields={fields}
        error={error}
        note="时间范围包含开始、不包含结束。只有查询功能，审计记录不提供修改或删除。"
      />
      <Panel
        title="审计记录"
        description="按发生时间倒序排列；人工发放不代表支付到账。"
        busy={query.isFetching}
      >
        {query.isError && query.data && (
          <ErrorState
            retained
            error={query.error}
            retry={() => void query.refetch()}
          />
        )}
        {error ? (
          <EmptyState>请修正筛选后查询。</EmptyState>
        ) : query.isPending ? (
          <Loading />
        ) : query.isError && !query.data ? (
          <ErrorState error={query.error} retry={() => void query.refetch()} />
        ) : (
          <>
            {!query.data.items.length ? (
              <EmptyState
                action={
                  params.size ? (
                    <button
                      className="admin-button"
                      onClick={() => setParams({})}
                    >
                      清除筛选条件
                    </button>
                  ) : undefined
                }
              >
                可以调整操作人、动作、对象或时间范围后重新查询。
              </EmptyState>
            ) : (
              <div className="admin-table-scroll">
                <table className="admin-table admin-records-table admin-audit-table">
                  <colgroup>
                    {Array.from({ length: 7 }, (_, index) => (
                      <col key={index} />
                    ))}
                  </colgroup>
                  <caption className="admin-sr-only">管理操作审计</caption>
                  <thead>
                    <tr>
                      {[
                        "发生时间（北京）",
                        "操作人",
                        "动作",
                        "对象",
                        "当时结果",
                        "操作原因",
                        "详情",
                      ].map((label) => (
                        <th scope="col" key={label}>
                          {label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {query.data.items.map((event) => (
                      <tr key={event.id}>
                        <td>
                          <DateTimeCell value={event.created_at} />
                        </td>
                        <td title={event.actor_label}>
                          {event.actor_kind === "user" &&
                          event.actor_user_id ? (
                            <CopyId
                              value={event.actor_user_id}
                              label="操作人编号"
                            />
                          ) : (
                            event.actor_label
                          )}
                        </td>
                        <td>{actionLabels[event.action] || event.action}</td>
                        <td title={event.target_id}>
                          <span className="admin-cell-note">
                            {targetLabels[event.target_type] ||
                              event.target_type}
                          </span>
                          <CopyId value={event.target_id} label="对象编号" />
                        </td>
                        <td>
                          <span
                            className={`admin-badge ${event.outcome === "succeeded" ? "admin-badge-completed" : event.outcome === "failed" ? "admin-badge-failed" : ""}`}
                          >
                            {outcomeLabels[event.outcome] || event.outcome}
                          </span>
                          {event.current_task_status && (
                            <small className="admin-cell-note">
                              当前任务：
                              <StatusBadge status={event.current_task_status} />
                            </small>
                          )}
                        </td>
                        <td>
                          <span
                            className="admin-audit-reason"
                            title={event.reason}
                          >
                            {event.reason}
                          </span>
                        </td>
                        <td>
                          <DetailDialog
                            title="审计详情"
                            description="操作发生时的事实与本次查询的任务状态分别展示。"
                            trigger={
                              <button
                                className="admin-text-button"
                                aria-label={`查看审计 ${shortId(event.id)}`}
                              >
                                查看
                              </button>
                            }
                          >
                            <AuditDetail event={event} />
                          </DetailDialog>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <Pagination
              total={query.data.total}
              offset={filters.offset || 0}
              label="审计"
              busy={query.isFetching}
              onPage={(offset) => {
                const next = new URLSearchParams(params);
                next.set("offset", String(offset));
                setParams(next);
              }}
            />
          </>
        )}
      </Panel>
    </>
  );
}
