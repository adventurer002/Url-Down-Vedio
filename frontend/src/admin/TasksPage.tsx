import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi } from "./api";
import {
  EmptyState,
  FilterForm,
  PageHeading,
  Pagination,
  readTaskFilters,
  taskFields,
  QuickFilters,
} from "./list";
import {
  ErrorState,
  DateTimeCell,
  CopyId,
  Loading,
  Panel,
  shortId,
  StatusBadge,
  taskLabels,
  PageSummary,
} from "./ui";
import { activeTask, waitLabels } from "./TaskDetail";
export default function TasksPage() {
  const [params, setParams] = useSearchParams();
  const { filters, error } = readTaskFilters(params);
  const query = useQuery({
    queryKey: ["admin", "tasks", filters],
    queryFn: ({ signal }) => adminApi.tasks(filters, signal),
    enabled: !error,
    refetchInterval: (query) =>
      query.state.data?.items.some((task) => activeTask(task.status))
        ? 5000
        : 30000,
  });
  function page(offset: number) {
    const next = new URLSearchParams(params);
    next.set("offset", String(offset));
    setParams(next);
  }
  const items = query.data?.items;
  return (
    <>
      <PageHeading
        title="任务管理"
        description="查看全局任务、执行进度与额度占用。"
      />
      <PageSummary
        busy={query.isFetching}
        items={[
          {
            label: "匹配任务",
            value: query.data?.total,
            note: "当前筛选的全部结果",
            icon: "text",
          },
          {
            label: "本页进行中",
            value: items?.filter((task) => activeTask(task.status)).length,
            note: "包括排队、执行和处理",
            icon: "clock",
          },
          {
            label: "本页已完成",
            value: items?.filter((task) => task.status === "completed").length,
            note: "已成功交付的任务",
            icon: "shield",
          },
          {
            label: "本页失败",
            value: items?.filter((task) => task.status === "failed").length,
            note: "可在详情中定位错误",
            icon: "search",
          },
        ]}
      />
      <QuickFilters
        items={[
          { label: "全部任务状态", values: { status: "" } },
          { label: "查看排队", values: { status: "queued" } },
          { label: "查看执行中", values: { status: "running" } },
          { label: "查看处理中", values: { status: "processing" } },
          { label: "定位失败", values: { status: "failed" } },
        ]}
      />
      <FilterForm
        key={params.toString()}
        fields={taskFields}
        error={error}
        note="按创建时间筛选，包含开始时刻，不包含结束时刻。所有时间均为北京时间。"
      />
      <Panel
        title="任务列表"
        description="查看全部用户及匿名任务的运行状态。有进行中任务时每 5 秒刷新，其余每 30 秒检查新任务。"
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
          <EmptyState>请修正筛选条件后查询。</EmptyState>
        ) : query.isPending ? (
          <Loading />
        ) : query.isError && !query.data ? (
          <ErrorState error={query.error} retry={() => void query.refetch()} />
        ) : (
          <>
            {query.data.items.length === 0 ? (
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
                可以调整任务状态、用户或时间范围后重新查询。
              </EmptyState>
            ) : (
              <div className="admin-table-scroll">
                <table className="admin-table admin-records-table admin-tasks-table">
                  <colgroup>
                    {Array.from({ length: 6 }, (_, index) => (
                      <col key={index} />
                    ))}
                  </colgroup>
                  <caption className="admin-sr-only">全局任务列表</caption>
                  <thead>
                    <tr>
                      {[
                        "任务",
                        "类型",
                        "用户编号",
                        "状态与进度",
                        "创建时间（北京）",
                        "操作",
                      ].map((label) => (
                        <th key={label} scope="col">
                          {label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {query.data.items.map((task) => (
                      <tr key={task.task_id}>
                        <td>
                          <div className="admin-task-title">
                            <div>
                              <strong title={task.video?.title}>
                                {task.video?.title ||
                                  taskLabels[task.task_type] ||
                                  "任务"}
                              </strong>
                              <CopyId value={task.task_id} label="任务编号" />
                              {(task.history_deleted ??
                                task.history_hidden) && (
                                <small>历史已删除</small>
                              )}
                            </div>
                          </div>
                        </td>
                        <td>{taskLabels[task.task_type] || task.task_type}</td>
                        <td>
                          {task.user_id ? (
                            <CopyId value={task.user_id} label="用户编号">
                              <Link
                                className="admin-text-button"
                                to={`/admin/users/${task.user_id}`}
                                title={task.user_id}
                              >
                                {shortId(task.user_id)}
                              </Link>
                            </CopyId>
                          ) : (
                            "匿名任务"
                          )}
                        </td>
                        <td>
                          <StatusBadge status={task.status} />
                          {activeTask(task.status) && (
                            <>
                              <span className="admin-progress-number">
                                {task.progress}%
                              </span>
                              <progress
                                className="admin-task-progress"
                                aria-label={`${shortId(task.task_id)} 任务进度`}
                                max={100}
                                value={task.progress}
                              />
                            </>
                          )}
                          {task.cancel_requested && (
                            <small className="admin-cell-note">
                              已请求取消
                            </small>
                          )}
                          {task.wait_reason && (
                            <small className="admin-cell-note">
                              {waitLabels[task.wait_reason] || task.wait_reason}
                            </small>
                          )}
                          {task.error_code && (
                            <small className="admin-cell-note">
                              {task.error_code}
                            </small>
                          )}
                        </td>
                        <td>
                          <DateTimeCell value={task.created_at} />
                        </td>
                        <td>
                          <Link
                            className="admin-text-button"
                            to={`/admin/tasks/${task.task_id}?${params}`}
                            aria-label={`查看任务 ${shortId(task.task_id)}`}
                          >
                            查看详情
                          </Link>
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
              onPage={page}
              label="任务"
              busy={query.isFetching}
            />
          </>
        )}
      </Panel>
    </>
  );
}
