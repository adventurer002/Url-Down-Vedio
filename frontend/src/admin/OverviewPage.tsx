import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { TaskDetail } from "./TaskDetail";
import { api } from "../api";
import { get } from "../api/client";
import Icon from "../components/Icon";
import { adminApi, type AdminTask, type TaskStatus } from "./api";
import {
  bytes,
  DetailDialog,
  ErrorState,
  formatTime,
  DateTimeCell,
  CopyId,
  Loading,
  Panel,
  shortId,
  StatusBadge,
  statusLabels,
  taskLabels,
} from "./ui";

export const adminKeys = {
  overview: ["admin", "overview"] as const,
  health: ["admin", "health"] as const,
  storage: ["admin", "storage"] as const,
  recent: ["admin", "recent"] as const,
};
function RecentTaskRow({ task }: { task: AdminTask }) {
  return (
    <tr>
      <td>
        <div className="admin-task-title">
          <span className="admin-task-icon">
            <Icon
              name={task.task_type.startsWith("ai_") ? "sparkles" : "text"}
              size={18}
            />
          </span>
          <div>
            <strong title={task.video?.title}>
              {task.video?.title || taskLabels[task.task_type] || "任务"}
            </strong>
            <CopyId value={task.task_id} label="任务编号" />
          </div>
        </div>
      </td>
      <td>{taskLabels[task.task_type] || task.task_type}</td>
      <td>
        {task.user_id ? (
          <CopyId value={task.user_id} label="用户编号" />
        ) : (
          "匿名任务"
        )}
      </td>
      <td>
        <StatusBadge status={task.status} />
        {task.cancel_requested && (
          <small className="admin-cell-note">已请求取消</small>
        )}
        {task.error_code && (
          <small className="admin-cell-note">{task.error_code}</small>
        )}
      </td>
      <td>
        <DateTimeCell value={task.created_at} />
      </td>
      <td>
        <DetailDialog
          title="任务详情"
          description="任务元数据；时间均为北京时间。"
          trigger={
            <button
              className="admin-text-button"
              aria-label={`查看任务 ${shortId(task.task_id)}`}
            >
              查看 <Icon name="arrow" size={14} />
            </button>
          }
        >
          <TaskDetail id={task.task_id} />
        </DetailDialog>
      </td>
    </tr>
  );
}
function RecentTasks() {
  const query = useQuery({
    queryKey: adminKeys.recent,
    queryFn: ({ signal }) => adminApi.recentTasks(signal),
    refetchInterval: 30000,
  });
  return (
    <Panel
      title="最近任务"
      busy={query.isFetching}
      description="最新 5 条 · 全部用户任务"
      className="admin-recent"
      action={
        <Link className="admin-text-button" to="/admin/tasks">
          全部任务 →
        </Link>
      }
    >
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : query.data.items.length === 0 ? (
        <div className="admin-empty">
          <Icon name="text" size={28} />
          <strong>暂无任务</strong>
          <p>新建任务后，最新记录会显示在这里。</p>
        </div>
      ) : (
        <div className="admin-table-scroll">
          <table className="admin-table admin-records-table admin-tasks-table">
            <colgroup>
              {Array.from({ length: 6 }, (_, index) => (
                <col key={index} />
              ))}
            </colgroup>
            <caption className="admin-sr-only">
              最近任务，按创建时间倒序排列
            </caption>
            <thead>
              <tr>
                {[
                  "任务",
                  "类型",
                  "用户编号",
                  "状态与说明",
                  "创建时间",
                  "操作",
                ].map((label) => (
                  <th scope="col" key={label}>
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {query.data.items.map((task) => (
                <RecentTaskRow key={task.task_id} task={task} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
export function ServiceHealth({
  showStorage = true,
}: {
  showStorage?: boolean;
}) {
  const query = useQuery({
    queryKey: adminKeys.health,
    queryFn: async ({ signal }) => {
      try {
        await api.ready(signal);
        return { api: "ready", dependencies: "ready" };
      } catch (error) {
        if (signal.aborted) throw error;
        try {
          await get("/health/live", signal);
          return { api: "ready", dependencies: "unknown" };
        } catch (failure) {
          if (signal.aborted) throw failure;
          return { api: "unavailable", dependencies: "unknown" };
        }
      }
    },
    refetchInterval: 30000,
  });
  const storage = useQuery({
    queryKey: adminKeys.storage,
    queryFn: ({ signal }) => adminApi.storage(signal),
    enabled: showStorage,
    staleTime: 60000,
    refetchOnWindowFocus: false,
  });
  return (
    <Panel
      title="服务状态"
      description="每 30 秒检查 · 存储按需刷新"
      busy={query.isFetching}
    >
      <div className="admin-service-list">
        {["API", "PostgreSQL", "Redis", "Worker"].map((name) => {
          const state = query.isPending
            ? "checking"
            : name === "Worker"
              ? "unknown"
              : name === "API"
                ? query.data?.api
                : query.data?.dependencies;
          return (
            <div className="admin-service-row" key={name}>
              <span>
                <Icon name={name === "Worker" ? "clock" : "server"} size={18} />
                {name}
              </span>
              <span className={`admin-health admin-health-${state}`}>
                {state === "checking" ? (
                  <span className="admin-spinner" aria-hidden="true" />
                ) : (
                  <i />
                )}
                {state === "ready"
                  ? "已就绪"
                  : state === "checking"
                    ? "检查中"
                    : state === "unavailable"
                      ? "无法连接"
                      : name === "Worker"
                        ? "未监测"
                        : "状态未知"}
              </span>
            </div>
          );
        })}
      </div>
      {query.data?.dependencies === "unknown" && (
        <p className="admin-footnote">
          就绪检查未通过，数据库与 Redis 的具体状态暂无法确认。
        </p>
      )}
      {showStorage && (
        <div className="admin-storage">
          <div className="admin-storage-heading">
            <span>
              <Icon name="storage" size={18} />
              存储空间
            </span>
            <span>
              {storage.data
                ? `${bytes(storage.data.used_bytes)} / ${bytes(storage.data.quota_bytes)}`
                : "—"}
            </span>
          </div>
          {storage.isPending ? (
            <Loading label="正在检查存储…" />
          ) : storage.isError ? (
            <ErrorState
              error={storage.error}
              retry={() => void storage.refetch()}
            />
          ) : (
            <>
              {storage.data.scan_complete && storage.data.used_bytes != null ? (
                <>
                  <progress
                    aria-label="存储配额占用"
                    max={Math.max(1, storage.data.quota_bytes)}
                    value={storage.data.used_bytes}
                  />
                  <p className="admin-footnote">
                    磁盘可用 {bytes(storage.data.disk_free_bytes)} · 文件保留{" "}
                    {storage.data.file_ttl_hours} 小时
                  </p>
                </>
              ) : (
                <p className="admin-footnote">
                  扫描未完成，已观察到 {bytes(storage.data.observed_bytes)}
                  ，总占用未知。
                </p>
              )}
            </>
          )}
        </div>
      )}
    </Panel>
  );
}
export default function OverviewPage() {
  const query = useQuery({
    queryKey: adminKeys.overview,
    queryFn: ({ signal }) => adminApi.overview(signal),
    refetchInterval: 30000,
  });
  const data = query.data;
  const statuses = Object.keys(statusLabels) as TaskStatus[];
  const cards = [
    {
      label: "今日新建任务",
      value: data?.tasks.created_today,
      icon: "text",
      note: "按北京时间创建日期统计",
    },
    {
      label: "执行中任务",
      value: data
        ? data.tasks.by_status.running + data.tasks.by_status.processing
        : undefined,
      icon: "clock",
      note: data
        ? `另有 ${data.tasks.by_status.queued} 项排队中`
        : "排队情况待获取",
    },
    {
      label: "用户总数",
      value: data?.users.total,
      icon: "user",
      note: data
        ? `${data.users.active} 个正常账号 · ${data.users.disabled} 个停用`
        : "账号状态待获取",
    },
    {
      label: "有效期内会员",
      value: data?.allowances.valid_memberships,
      icon: "shield",
      note: "按会员有效期统计",
    },
  ];
  return (
    <>
      <div className="admin-page-heading">
        <div>
          <div className="admin-title-line">
            <h1>数据概览</h1>
            <span
              className={`admin-live-label${query.isError || query.isPending ? " admin-live-muted" : ""}`}
            >
              <i />
              {query.isPending
                ? "加载中"
                : query.isError
                  ? "更新失败"
                  : "实时数据"}
            </span>
          </div>
          <p>关注任务与用户，了解平台运行情况。</p>
        </div>
        <span className="admin-date">
          {data?.business_date || "今日统计"} · UTC+8
        </span>
      </div>
      {query.isError && (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      )}
      {query.isPending && <Loading label="正在加载概览…" compact />}
      <div className="admin-metrics">
        {cards.map((card) => (
          <section className="admin-metric" key={card.label}>
            <span className="admin-metric-icon">
              <Icon name={card.icon} size={22} />
            </span>
            <div>
              <h2>{card.label}</h2>
              <strong aria-label={`${card.label}：${card.value ?? "未知"}`}>
                {query.isPending ? (
                  <span className="admin-skeleton-value" aria-hidden="true" />
                ) : card.value == null ? (
                  "—"
                ) : (
                  card.value.toLocaleString("zh-CN")
                )}
              </strong>
              <p>{card.note}</p>
            </div>
          </section>
        ))}
      </div>
      <div className="admin-info-strip">
        <Icon name="shield" size={17} />
        <p>当前为人工权益发放模式，尚未接入真实支付。</p>
        <span>统计截至 {formatTime(data?.as_of)}</span>
      </div>
      <div className="admin-grid">
        <Panel
          title="任务状态分布"
          busy={query.isFetching}
          description="全部历史任务 · 按当前状态统计"
          action={
            <span className="admin-subtle">
              共 {data?.tasks.total?.toLocaleString("zh-CN") ?? "—"} 项
            </span>
          }
        >
          {!data ? (
            query.isPending ? (
              <Loading label="正在读取任务统计…" />
            ) : (
              <div className="admin-empty">
                <p>任务统计暂不可用</p>
              </div>
            )
          ) : (
            <div className="admin-distribution">
              {statuses.map((status) => (
                <div className="admin-distribution-row" key={status}>
                  <span>{statusLabels[status]}</span>
                  <div className="admin-bar-track">
                    <div
                      className={`admin-bar admin-bar-${status}`}
                      style={{
                        width: `${data.tasks.total ? (data.tasks.by_status[status] / data.tasks.total) * 100 : 0}%`,
                      }}
                    />
                  </div>
                  <strong>
                    {data.tasks.by_status[status].toLocaleString("zh-CN")}
                  </strong>
                </div>
              ))}
              {data.tasks.total === 0 && (
                <p className="admin-footnote">暂无历史任务，所有计数为 0。</p>
              )}
            </div>
          )}
        </Panel>
        <ServiceHealth />
      </div>
      <nav className="admin-attention" aria-label="待关注事项">
        <Link to="/admin/tasks?status=failed">
          <Icon name="search" size={20} />
          <div>
            <strong>检查失败任务</strong>
            <small>
              {data
                ? `${data.tasks.by_status.failed} 项历史失败记录`
                : "查看错误代码与执行记录"}
            </small>
          </div>
          <Icon name="arrow" size={16} />
        </Link>
        <Link to="/admin/tasks?status=queued">
          <Icon name="clock" size={20} />
          <div>
            <strong>查看排队情况</strong>
            <small>
              {data
                ? `${data.tasks.by_status.queued} 项正在等待`
                : "查看任务等待原因"}
            </small>
          </div>
          <Icon name="arrow" size={16} />
        </Link>
        <Link to="/admin/users?status=disabled">
          <Icon name="user" size={20} />
          <div>
            <strong>核对停用账号</strong>
            <small>
              {data
                ? `${data.users.disabled} 个账号已停用`
                : "查看账号与权益状态"}
            </small>
          </div>
          <Icon name="arrow" size={16} />
        </Link>
      </nav>
      <RecentTasks />
    </>
  );
}
