import GrantAction from "./GrantAction";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { adminApi, type AllowanceKind } from "./api";
import {
  EmptyState,
  isUuid,
  PageHeading,
  pageOffset,
  Pagination,
} from "./list";
import { bytes, ErrorState, formatDateTime, Loading, Panel } from "./ui";
import { useAdminUser, UserLinks } from "./UserDetail";
export function quantity(kind: AllowanceKind, amount: number) {
  return kind === "asr_seconds"
    ? `${Math.floor(amount / 60)} 分 ${amount % 60} 秒`
    : `${amount} 次`;
}
const sourceLabels: Record<string, string> = {
  trial: "首次体验",
  monthly: "月会员",
  asr_120: "转写加购",
  ai_100: "AI 加购",
};
const eventLabels: Record<string, string> = {
  grant: "发放权益",
  hold: "执行占用",
  settle: "批次核销",
  release: "批次释放",
  settled: "任务已结算",
  released: "任务已释放",
  expire: "批次过期",
  asr_usage: "转写计量",
};
const detailLabels: Record<string, string> = {
  source: "权益来源",
  asr_seconds: "转写秒数",
  ai_generations: "AI 次数",
  starts_at: "生效时间",
  expires_at: "过期时间",
  task_id: "任务编号",
  allowance_id: "批次编号",
  consumed: "核销量",
  released: "释放量",
  release_expired: "释放时已过期",
  already_owned: "已有相同结果",
  measured_seconds: "实测转写秒数",
  user_max_seconds: "确认转写上限",
  platform_seconds: "平台承担秒数",
  kind: "额度类型",
  remaining: "剩余量",
  amount: "数量",
};
function Ledger({ id }: { id: string }) {
  const [params, setParams] = useSearchParams();
  const offset = pageOffset(params, "ledger_offset");
  const query = useQuery({
    queryKey: ["admin", "ledger", id, offset],
    queryFn: ({ signal }) => adminApi.ledger(id, offset, signal),
  });
  return (
    <Panel
      title="额度流水"
      description="权益发放、占用、核销和释放记录；不表示真实支付到账。"
    >
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          {!query.data.items.length ? (
            <EmptyState>尚无额度流水。</EmptyState>
          ) : (
            <div className="admin-ledger-list">
              {query.data.items.map((event) => (
                <article className="admin-ledger-event" key={event.id}>
                  <div>
                    <strong>{eventLabels[event.event] || event.event}</strong>
                    <time>{formatDateTime(event.created_at)}</time>
                  </div>
                  <small className="admin-wrap">{event.id}</small>
                  <dl className="admin-ledger-details">
                    {Object.entries(event.detail).map(([key, value]) => (
                      <div key={key}>
                        <dt>{detailLabels[key] || key}</dt>
                        <dd>
                          {key.endsWith("_at") && typeof value === "string"
                            ? formatDateTime(value)
                            : typeof value === "boolean"
                              ? value
                                ? "是"
                                : "否"
                              : typeof value === "string" ||
                                  typeof value === "number"
                                ? String(value)
                                : value == null
                                  ? "—"
                                  : "结构化记录"}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </article>
              ))}
            </div>
          )}
          <Pagination
            total={query.data.total}
            offset={offset}
            onPage={(offset) => {
              const next = new URLSearchParams(params);
              next.set("ledger_offset", String(offset));
              setParams(next);
            }}
            label="流水"
          />
        </>
      )}
    </Panel>
  );
}
export default function AllowancesDetailPage() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const offset = pageOffset(params, "lot_offset");
  const user = useAdminUser(id);
  const [ledgerOpen, setLedgerOpen] = useState(false);
  const query = useQuery({
    queryKey: ["admin", "allowances", id, offset],
    queryFn: ({ signal }) => adminApi.allowances(id, offset, signal),
    enabled: isUuid(id),
  });
  const data = query.data;
  return (
    <>
      <PageHeading
        title="会员与额度详情"
        description={
          user.data
            ? `${user.data.nickname} · ${user.data.id}`
            : "查看当前余额、执行占用与权益批次。"
        }
        action={
          <Link className="admin-button" to={`/admin/allowances?${params}`}>
            返回权益用户列表
          </Link>
        }
      />
      {!isUuid(id) ? (
        <p role="alert">用户编号无效，需要完整 UUID。</p>
      ) : (
        <>
          <UserLinks id={id} allowances />
          {user.data && !user.isError && (
            <div className="admin-detail-actions">
              <GrantAction user={user.data} />
            </div>
          )}
          {user.isError && (
            <ErrorState error={user.error} retry={() => void user.refetch()} />
          )}
          {query.isPending ? (
            <Loading />
          ) : query.isError ? (
            <ErrorState
              error={query.error}
              retry={() => void query.refetch()}
            />
          ) : (
            data && (
              <>
                <div className="admin-info-strip">
                  <p>
                    {data.account_exists
                      ? `会员：${data.member ? "有效期内" : "无有效会员"} · 到期 ${formatDateTime(data.member_until)} · ${data.account_active ? "账号正常" : "账号停用"} · ${data.subscribed ? "已关注" : "未关注"}`
                      : "尚未建立额度账户；本页仅查询，不会创建账户或领取体验权益。"}
                  </p>
                  <span>截至 {formatDateTime(data.as_of)}</span>
                </div>
                <div className="admin-allowance-metrics">
                  {(["asr_seconds", "ai_generations"] as const).map((kind) => (
                    <Panel
                      key={kind}
                      title={kind === "asr_seconds" ? "转写额度" : "AI 额度"}
                    >
                      <p className="admin-subtle">当前可用</p>
                      <strong className="admin-quota-value">
                        {quantity(kind, data.balances[kind].usable)}
                      </strong>
                      <dl className="admin-quota-meta">
                        <dt>有效剩余</dt>
                        <dd>{quantity(kind, data.balances[kind].available)}</dd>
                        <dt>执行占用</dt>
                        <dd>{quantity(kind, data.balances[kind].held)}</dd>
                      </dl>
                    </Panel>
                  ))}
                </div>
                <p className="admin-footnote admin-quota-note">
                  当前可用会考虑账号状态；有效剩余不含未来批次，未关注时排除体验额度；执行占用包含尚未结算的过期批次。
                </p>
                <div className="admin-allowance-metrics">
                  <Panel
                    title={`当日用量 · ${data.daily.date}`}
                    description="北京时间业务日"
                  >
                    <dl className="admin-details">
                      <dt>成功下载</dt>
                      <dd>{data.daily.downloads_used} 次</dd>
                      <dt>下载占用</dt>
                      <dd>{data.daily.downloads_held} 次</dd>
                      <dt>已用下行</dt>
                      <dd>{bytes(data.daily.egress_used_bytes)}</dd>
                      <dt>下行占用</dt>
                      <dd>{bytes(data.daily.egress_held_bytes)}</dd>
                    </dl>
                  </Panel>
                  <Panel
                    title="资源限制"
                    description="账号额度上限；实际任务仍由 Solo Worker 执行。"
                  >
                    <dl className="admin-details">
                      <dt>视频上限</dt>
                      <dd>
                        {quantity("asr_seconds", data.limits.video_seconds)}
                      </dd>
                      <dt>单文件上限</dt>
                      <dd>{bytes(data.limits.file_bytes)}</dd>
                      <dt>每日下行</dt>
                      <dd>{bytes(data.limits.daily_egress_bytes)}</dd>
                      <dt>任务并发额度</dt>
                      <dd>{data.limits.download_concurrency}</dd>
                    </dl>
                  </Panel>
                </div>
                <Panel
                  title="权益批次"
                  description="按过期时间排序；未生效、过期和使用中批次均保留展示。"
                >
                  {data.lots.items.length === 0 ? (
                    <EmptyState>尚无权益批次。</EmptyState>
                  ) : (
                    <div className="admin-table-scroll">
                      <table className="admin-table">
                        <caption className="admin-sr-only">权益批次</caption>
                        <thead>
                          <tr>
                            {[
                              "类型 / 来源",
                              "发放",
                              "已用",
                              "占用",
                              "当前可用",
                              "生效 / 过期（北京）",
                              "批次状态",
                            ].map((label) => (
                              <th key={label} scope="col">
                                {label}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {data.lots.items.map((lot) => (
                            <tr key={lot.id}>
                              <td>
                                {lot.kind === "asr_seconds" ? "转写" : "AI"}
                                <small className="admin-cell-note">
                                  {sourceLabels[lot.source] || lot.source}
                                </small>
                              </td>
                              <td>{quantity(lot.kind, lot.granted)}</td>
                              <td>{quantity(lot.kind, lot.used)}</td>
                              <td>{quantity(lot.kind, lot.held)}</td>
                              <td>{quantity(lot.kind, lot.usable)}</td>
                              <td>
                                {formatDateTime(lot.starts_at)}
                                <small className="admin-cell-note">
                                  至 {formatDateTime(lot.expires_at)}
                                </small>
                              </td>
                              <td>
                                <span className="admin-badge">
                                  {lot.expired
                                    ? "已过期"
                                    : lot.future
                                      ? "未生效"
                                      : "有效期内"}
                                </span>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  <Pagination
                    total={data.lots.total}
                    offset={offset}
                    onPage={(offset) => {
                      const next = new URLSearchParams(params);
                      next.set("lot_offset", String(offset));
                      setParams(next);
                    }}
                    label="批次"
                  />
                </Panel>
              </>
            )
          )}
          <div className="admin-ledger-toggle">
            <button
              className="admin-button"
              aria-expanded={ledgerOpen}
              aria-controls="admin-user-ledger"
              onClick={() => setLedgerOpen(!ledgerOpen)}
            >
              {ledgerOpen ? "收起额度流水" : "查看额度流水"}
            </button>
          </div>
          {ledgerOpen && (
            <div id="admin-user-ledger">
              <Ledger id={id} />
            </div>
          )}
        </>
      )}
    </>
  );
}
