import { membershipLabel } from "../utils/billing";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi } from "./api";
import {
  EmptyState,
  FilterForm,
  PageHeading,
  Pagination,
  readUserFilters,
  userFields,
  QuickFilters,
} from "./list";
import {
  ErrorState,
  DateTimeCell,
  CopyId,
  Loading,
  Panel,
  PageSummary,
} from "./ui";
export function AccountStatus({ status }: { status: string }) {
  return (
    <span
      className={`admin-badge ${status === "active" ? "admin-badge-completed" : "admin-badge-failed"}`}
    >
      {status === "active" ? "正常" : status === "disabled" ? "停用" : status}
    </span>
  );
}
export default function UsersPage({
  allowances = false,
}: {
  allowances?: boolean;
}) {
  const [params, setParams] = useSearchParams();
  const { filters, error } = readUserFilters(params);
  const query = useQuery({
    queryKey: ["admin", "users", filters],
    queryFn: ({ signal }) => adminApi.users(filters, signal),
    enabled: !error,
  });
  const base = allowances ? "/admin/allowances" : "/admin/users";
  const items = query.data?.items;
  const count = (
    predicate: (user: NonNullable<typeof items>[number]) => boolean,
  ) => items?.filter(predicate).length;
  const clearFilters = (
    <button className="admin-button" onClick={() => setParams({})}>
      清除筛选条件
    </button>
  );
  function page(offset: number) {
    const next = new URLSearchParams(params);
    next.set("offset", String(offset));
    setParams(next);
  }
  return (
    <>
      <PageHeading
        title={allowances ? "会员与额度" : "用户管理"}
        description={
          allowances
            ? "按用户查看会员期限、额度余额、权益批次与流水。"
            : "查看账号信息、关注情况与会员等级。"
        }
      />
      <PageSummary
        busy={query.isFetching}
        items={[
          {
            label: "匹配用户",
            value: query.data?.total,
            note: "当前筛选的全部结果",
            icon: "user",
          },
          {
            label: "本页正常账号",
            value: count((user) => user.status === "active"),
            note: "仅统计当前页账号",
            icon: "shield",
          },
          {
            label: "本页会员",
            value: count((user) => user.member),
            note: "会员仍在有效期内",
            icon: "bookmark",
          },
          {
            label: "本页未关注",
            value: count((user) => !user.is_subscribed),
            note: "与会员和账号状态分开",
            icon: "chat",
          },
        ]}
      />
      <QuickFilters
        items={[
          { label: "全部账号状态", values: { status: "" } },
          { label: "停用账号", values: { status: "disabled" } },
          { label: "有效会员", values: { member: "true" } },
          { label: "无有效会员", values: { member: "false" } },
          { label: "未关注账号", values: { subscribed: "false" } },
        ]}
      />
      <FilterForm key={`${base}:${params}`} fields={userFields} error={error} />
      {allowances && (
        <details className="admin-list-guide">
          <summary>人工权益处理说明</summary>
          <Panel title="人工权益处理" className="admin-guide">
            <ol>
              <li>
                <span>1</span>
                <div>
                  <strong>选择目标用户</strong>
                  <p>核对账号、会员有效期与当前余额。</p>
                </div>
              </li>
              <li>
                <span>2</span>
                <div>
                  <strong>预览固定产品</strong>
                  <p>Plus、Pro 月会员、转写包或 AI 包，先确认生效时间。</p>
                </div>
              </li>
              <li>
                <span>3</span>
                <div>
                  <strong>填写原因并确认</strong>
                  <p>人工发放保留审计记录，不表示支付到账。</p>
                </div>
              </li>
            </ol>
          </Panel>
        </details>
      )}
      <Panel
        title={allowances ? "选择用户查看权益" : "用户列表"}
        description="显示当前生效的会员等级与有效期。"
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
              <EmptyState action={params.size > 0 ? clearFilters : undefined}>
                可以调整昵称、账号或会员条件后重新查询。
              </EmptyState>
            ) : (
              <div className="admin-table-scroll">
                <table className="admin-table admin-records-table admin-users-table">
                  <colgroup>
                    {Array.from({ length: 8 }, (_, index) => (
                      <col key={index} />
                    ))}
                  </colgroup>
                  <caption className="admin-sr-only">
                    {allowances ? "会员额度用户列表" : "用户列表"}
                  </caption>
                  <thead>
                    <tr>
                      {[
                        "用户",
                        "角色",
                        "账号状态",
                        "公众号关注",
                        "会员等级",
                        "会员到期（北京）",
                        "最近登录（北京）",
                        "操作",
                      ].map((label) => (
                        <th scope="col" key={label}>
                          {label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {query.data.items.map((user) => (
                      <tr key={user.id}>
                        <td>
                          <div className="admin-task-title">
                            <div>
                              <strong title={user.nickname}>
                                {user.nickname}
                              </strong>
                              <CopyId value={user.id} label="用户编号" />
                            </div>
                          </div>
                        </td>
                        <td>{user.role === "admin" ? "管理员" : "用户"}</td>
                        <td>
                          <AccountStatus status={user.status} />
                        </td>
                        <td>{user.is_subscribed ? "已关注" : "未关注"}</td>
                        <td>
                          <span
                            className={`admin-badge ${user.member ? "admin-badge-completed" : ""}`}
                          >
                            {membershipLabel(user.member_tier, user.member)}
                          </span>
                        </td>
                        <td>
                          <DateTimeCell value={user.member_until} />
                        </td>
                        <td>
                          <DateTimeCell value={user.last_login_at} />
                        </td>
                        <td>
                          <Link
                            className="admin-text-button"
                            to={`${base}/${user.id}?${params}`}
                            aria-label={`${allowances ? "查看额度" : "查看用户"} ${user.nickname}`}
                          >
                            {allowances ? "查看权益" : "查看详情"}
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
              label="用户"
              busy={query.isFetching}
            />
          </>
        )}
      </Panel>
    </>
  );
}
