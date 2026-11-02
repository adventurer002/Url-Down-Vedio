import { useState, type FormEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { statusLabels, taskLabels } from "./ui";
import type { TaskFilters, UserFilters } from "./api";
import Icon from "../components/Icon";

export function QuickFilters({
  items,
}: {
  items: { label: string; values: Record<string, string> }[];
}) {
  const [params, setParams] = useSearchParams();
  return (
    <nav className="admin-quick-filters" aria-label="常用筛选">
      <span>
        <Icon name="search" size={14} />
        快捷查看
      </span>
      {items.map((item) => (
        <button
          key={item.label}
          className="admin-filter-chip"
          type="button"
          aria-pressed={Object.entries(item.values).every(
            ([key, value]) => (params.get(key) || "") === value,
          )}
          onClick={() => {
            const next = new URLSearchParams(params);
            next.delete("offset");
            const selected = Object.entries(item.values).every(
              ([key, value]) => (params.get(key) || "") === value,
            );
            Object.entries(item.values).forEach(([key, value]) =>
              value && !selected ? next.set(key, value) : next.delete(key),
            );
            setParams(next);
          }}
        >
          {item.label}
        </button>
      ))}
    </nav>
  );
}

export const isUuid = (value: string) =>
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
export function pageOffset(params: URLSearchParams, key = "offset") {
  const raw = params.get(key) || "0";
  return /^\d+$/.test(raw) && Number.isSafeInteger(Number(raw))
    ? Number(raw)
    : 0;
}
function optionError(value: string | null, values: string[]) {
  return Boolean(value && !values.includes(value));
}
export function readTaskFilters(params: URLSearchParams): {
  filters: TaskFilters;
  error: string;
} {
  const filters: TaskFilters = {
    limit: 20,
    offset: pageOffset(params),
    status: params.get("status") || "",
    task_type: params.get("task_type") || "",
    user_id: params.get("user_id")?.trim() || "",
  };
  if (
    optionError(filters.status || null, Object.keys(statusLabels)) ||
    optionError(filters.task_type || null, Object.keys(taskLabels))
  )
    return { filters, error: "任务状态或类型无效，请重新选择。" };
  if (filters.user_id && !isUuid(filters.user_id))
    return { filters, error: "用户编号需要填写完整 UUID。" };
  for (const key of ["since", "until"] as const) {
    const raw = params.get(key);
    if (!raw) continue;
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(raw))
      return { filters, error: "请输入有效的北京时间。" };
    const instant = new Date(`${raw}:00+08:00`);
    // Reject normalized dates such as February 30, not just Invalid Date.
    if (
      !Number.isFinite(instant.getTime()) ||
      new Date(instant.getTime() + 8 * 3600000).toISOString().slice(0, 16) !==
        raw
    )
      return { filters, error: "请输入有效的北京时间。" };
    filters[key] = instant.toISOString();
  }
  if (filters.since && filters.until && filters.since >= filters.until)
    return { filters, error: "开始时间必须早于结束时间。" };
  return { filters, error: "" };
}
export function readUserFilters(params: URLSearchParams): {
  filters: UserFilters;
  error: string;
} {
  const filters: UserFilters = {
    limit: 20,
    offset: pageOffset(params),
    q: params.get("q")?.trim() || "",
    status: params.get("status") || "",
    subscribed: params.get("subscribed") || "",
    member: params.get("member") || "",
  };
  const error =
    (filters.q?.length || 0) > 100
      ? "昵称或用户编号最多 100 个字符。"
      : optionError(filters.status || null, ["active", "disabled"]) ||
          optionError(filters.subscribed || null, ["true", "false"]) ||
          optionError(filters.member || null, ["true", "false"])
        ? "筛选条件无效，请重新选择。"
        : "";
  return { filters, error };
}
export interface FilterField {
  name: string;
  label: string;
  type?: "datetime-local";
  placeholder?: string;
  options?: Record<string, string>;
  maxLength?: number;
  advanced?: boolean;
}
export function FilterForm({
  fields,
  error,
  note,
}: {
  fields: FilterField[];
  error?: string;
  note?: string;
}) {
  const [params, setParams] = useSearchParams();
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      fields.map((field) => [field.name, params.get(field.name) || ""]),
    ),
  );
  const advancedFields = fields.filter((field) => field.advanced);
  const appliedAdvanced = advancedFields.filter((field) =>
    params.get(field.name)?.trim(),
  ).length;
  const [advancedOpen, setAdvancedOpen] = useState(appliedAdvanced > 0);
  function renderField(field: FilterField) {
    return (
      <label key={field.name}>
        {field.label}
        {field.options ? (
          <select
            value={values[field.name] || ""}
            onChange={(event) =>
              setValues({ ...values, [field.name]: event.target.value })
            }
          >
            <option value="">全部</option>
            {Object.entries(field.options).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        ) : (
          <input
            type={field.type || "text"}
            value={values[field.name] || ""}
            maxLength={field.maxLength}
            placeholder={field.placeholder}
            onChange={(event) =>
              setValues({ ...values, [field.name]: event.target.value })
            }
          />
        )}
      </label>
    );
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    const next = new URLSearchParams();
    for (const field of fields)
      if (values[field.name]?.trim())
        next.set(field.name, values[field.name]!.trim());
    setParams(next);
  }
  return (
    <form className="admin-filters" onSubmit={submit} aria-label="列表筛选">
      <div className="admin-filter-fields">
        {fields.filter((field) => !field.advanced).map(renderField)}
        <div className="admin-filter-actions">
          <button className="admin-button admin-button-primary" type="submit">
            查询
          </button>
          <button
            className="admin-button"
            type="button"
            onClick={() => {
              setValues({});
              setAdvancedOpen(false);
              setParams({});
            }}
          >
            重置
          </button>
        </div>
      </div>
      {advancedFields.length > 0 && (
        <details
          className="admin-advanced-filters"
          open={advancedOpen}
          onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
        >
          <summary>
            更多筛选
            {appliedAdvanced > 0 && <span>已应用 {appliedAdvanced} 项</span>}
          </summary>
          <div className="admin-filter-fields">
            {advancedFields.map(renderField)}
          </div>
          {note && <p className="admin-footnote">{note}</p>}
        </details>
      )}
      {note && !advancedFields.length && (
        <p className="admin-footnote">{note}</p>
      )}
      {error && (
        <p className="admin-form-error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
export function Pagination({
  total,
  offset,
  limit = 20,
  onPage,
  label = "列表",
  busy = false,
}: {
  total: number;
  offset: number;
  limit?: number;
  onPage: (offset: number) => void;
  label?: string;
  busy?: boolean;
}) {
  return (
    <nav className="admin-pagination" aria-label={`${label}分页`}>
      <span>
        共 {total} 条
        {total > 0 && offset < total
          ? ` · 第 ${offset + 1}–${Math.min(offset + limit, total)} 条`
          : ""}
      </span>
      <div>
        {offset >= total && offset > 0 && (
          <button
            className="admin-button"
            onClick={() => onPage(0)}
            disabled={busy}
          >
            返回第一页
          </button>
        )}
        <button
          className="admin-button"
          disabled={busy || offset === 0}
          onClick={() => onPage(Math.max(0, offset - limit))}
        >
          上一页
        </button>
        <span>第 {Math.floor(offset / limit) + 1} 页</span>
        <button
          className="admin-button"
          disabled={busy || offset + limit >= total}
          onClick={() => onPage(offset + limit)}
        >
          下一页
        </button>
      </div>
    </nav>
  );
}
export function EmptyState({
  children = "没有符合条件的记录。",
  action,
}: {
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="admin-empty">
      <span className="admin-empty-icon">
        <Icon name="search" size={25} />
      </span>
      <strong>暂无记录</strong>
      <p>{children}</p>
      {action}
    </div>
  );
}
export function PageHeading({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="admin-page-heading">
      <div className="admin-list-heading">
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </div>
  );
}
export const taskFields: FilterField[] = [
  { name: "status", label: "任务状态", options: statusLabels },
  { name: "task_type", label: "任务类型", options: taskLabels },
  {
    name: "user_id",
    label: "用户编号",
    placeholder: "完整 UUID",
    advanced: true,
  },
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
export const userFields: FilterField[] = [
  {
    name: "q",
    label: "昵称或用户编号",
    placeholder: "昵称 / 完整 UUID",
    maxLength: 100,
  },
  {
    name: "status",
    label: "账号状态",
    options: { active: "正常", disabled: "停用" },
  },
  {
    name: "subscribed",
    label: "公众号关注",
    options: { true: "已关注", false: "未关注" },
  },
  {
    name: "member",
    label: "会员状态",
    options: { true: "有效期内", false: "无有效会员" },
  },
];
