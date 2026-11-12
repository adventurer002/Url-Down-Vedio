import { membershipLabel } from "../utils/billing";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useParams } from "react-router-dom";
import { adminApi } from "./api";
import { isUuid, PageHeading } from "./list";
import { ErrorState, formatDateTime, Loading, Panel } from "./ui";
import StatusAction from "./StatusAction";
import { AccountStatus } from "./UsersPage";
export function useAdminUser(id: string) {
  return useQuery({
    queryKey: ["admin", "user", id],
    queryFn: ({ signal }) => adminApi.user(id, signal),
    enabled: isUuid(id),
  });
}
export function UserLinks({
  id,
  allowances = false,
}: {
  id: string;
  allowances?: boolean;
}) {
  const { search } = useLocation();
  return (
    <nav className="admin-detail-nav" aria-label="用户详情导航">
      <Link
        className={allowances ? "" : "admin-detail-nav-active"}
        aria-current={!allowances ? "page" : undefined}
        to={`/admin/users/${id}${search}`}
      >
        用户信息
      </Link>
      <Link
        className={allowances ? "admin-detail-nav-active" : ""}
        aria-current={allowances ? "page" : undefined}
        to={`/admin/allowances/${id}${search}`}
      >
        会员与额度
      </Link>
      <Link to={`/admin/tasks?user_id=${id}`}>该用户的任务 →</Link>
      <Link to={`/admin/audit?target_type=user&target_id=${id}`}>
        相关操作审计 →
      </Link>
    </nav>
  );
}
export default function UserDetailPage() {
  const { id = "" } = useParams();
  const { search } = useLocation();
  const query = useAdminUser(id);
  return (
    <>
      <PageHeading
        title="用户详情"
        description="账号、关注与会员分别记录，停用不清空已有权益。"
        action={
          <Link className="admin-button" to={`/admin/users${search}`}>
            返回用户列表
          </Link>
        }
      />
      {!isUuid(id) ? (
        <p role="alert">用户编号无效，需要完整 UUID。</p>
      ) : (
        <>
          <UserLinks id={id} />
          <Panel title="账号信息" className="admin-detail-panel">
            {query.isPending ? (
              <Loading />
            ) : query.isError ? (
              <ErrorState
                error={query.error}
                retry={() => void query.refetch()}
              />
            ) : (
              <>
                <div className="admin-detail-actions">
                  <StatusAction user={query.data} />
                </div>
                <dl className="admin-details">
                  <dt>用户编号</dt>
                  <dd>{query.data.id}</dd>
                  <dt>昵称</dt>
                  <dd>{query.data.nickname}</dd>
                  <dt>角色</dt>
                  <dd>{query.data.role === "admin" ? "管理员" : "用户"}</dd>
                  <dt>账号状态</dt>
                  <dd>
                    <AccountStatus status={query.data.status} />
                  </dd>
                  <dt>状态版本</dt>
                  <dd>{query.data.status_version}</dd>
                  <dt>公众号关注</dt>
                  <dd>{query.data.is_subscribed ? "已关注" : "未关注"}</dd>
                  <dt>会员等级</dt>
                  <dd>
                    {membershipLabel(query.data.member_tier, query.data.member)}
                  </dd>
                  <dt>会员到期</dt>
                  <dd>{formatDateTime(query.data.member_until)}</dd>
                  <dt>注册时间</dt>
                  <dd>{formatDateTime(query.data.created_at)}</dd>
                  <dt>最近登录</dt>
                  <dd>{formatDateTime(query.data.last_login_at)}</dd>
                </dl>
              </>
            )}
          </Panel>
        </>
      )}
    </>
  );
}
