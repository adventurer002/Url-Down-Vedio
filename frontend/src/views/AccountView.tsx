import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { normalizeError, type ApiError } from "../api/client";
import { authStore, useAuth } from "../stores/auth";
import { billingStore, useBilling } from "../stores/billing";
import { useWorkspaceStore } from "../hooks/useWorkspace";
import type { BillingCatalog } from "../types/billing";
import {
  allowanceSourceLabel,
  dateLabel,
  durationLabel,
} from "../utils/billing";
import ErrorNotice from "../components/ErrorNotice";
import Icon from "../components/Icon";

function trafficLabel(bytes: number) {
  return bytes >= 1024 ** 3
    ? `${Number((bytes / 1024 ** 3).toFixed(1))} GiB`
    : `${Number((bytes / 1024 ** 2).toFixed(1))} MiB`;
}

export default function AccountView() {
  const auth = useAuth();
  return (
    <div className="account-page">
      <nav className="subscription-breadcrumb" aria-label="面包屑">
        <a href="/">首页</a>
        <span>/</span>
        <span>个人中心</span>
      </nav>
      <header className="account-heading">
        <h1>个人中心</h1>
        <p>管理你的账户、权益与使用额度。</p>
      </header>
      {auth.restoring ? (
        <p role="status" className="account-empty">
          正在恢复登录状态…
        </p>
      ) : auth.restoreError ? (
        <div className="account-empty">
          <p>登录状态暂时无法恢复，请重新连接后查看个人信息。</p>
          <button
            className="button secondary"
            onClick={() => void authStore.restore()}
          >
            重新连接
          </button>
        </div>
      ) : auth.user?.status === "active" && authStore.hasAccess() ? (
        <AccountSpace key={auth.user.id} />
      ) : (
        <section className="account-empty">
          <Icon name="user" size={32} />
          <h2>登录后查看你的账户</h2>
          <p>在这里查看会员权益、可用额度和使用记录。</p>
          <button className="button primary" onClick={authStore.open}>
            登录查看个人中心
          </button>
        </section>
      )}
    </div>
  );
}

function AccountSpace() {
  const { user } = useAuth();
  const billing = useBilling();
  const store = useWorkspaceStore();
  const [catalog, setCatalog] = useState<BillingCatalog>();
  const [catalogError, setCatalogError] = useState<ApiError | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setCatalogError(null);
    void api
      .catalog(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setCatalog(data);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setCatalogError(normalizeError(cause));
      });
    return () => controller.abort();
  }, [attempt]);
  useEffect(() => {
    void billingStore.refresh({ ifStale: true });
  }, []);
  const data = billing.data;
  const subscribed = data?.subscribed ?? user?.is_subscribed;
  const currentAllowances =
    data?.allowances.filter(
      (item) =>
        !item.expired && new Date(item.expires_at).getTime() > Date.now(),
    ) ?? [];
  return (
    <>
      <section className="account-profile" aria-label="账户与会员状态">
        <div className="account-profile-identity">
          <span className="account-avatar">
            <Icon name="user" size={25} />
          </span>
          <div>
            <h2>{user?.nickname || "微信用户"}</h2>
            <p>
              <Icon name={subscribed ? "shield" : "user"} size={15} />
              {subscribed ? "公众号已关注" : "公众号未关注"}
            </p>
          </div>
        </div>
        <div className={"account-membership" + (data?.member ? " member" : "")}>
          <p>当前权益</p>
          <strong>
            {data ? (data.member ? "月度会员" : "免费用户") : "权益暂未读取"}
          </strong>
          <p>
            {data?.member
              ? `有效至 ${dateLabel(data.member_until)}`
              : "转写与 AI 额度分别计算"}
          </p>
        </div>
        <Link className="button secondary" to="/subscription">
          查看订阅方案
          <Icon name="arrow" size={16} />
        </Link>
      </section>

      <section
        className="account-allowances"
        aria-labelledby="account-allowance-title"
        aria-busy={billing.loading}
      >
        <div className="account-section-heading">
          <h2 id="account-allowance-title">我的额度</h2>
          <button
            className="text-button"
            disabled={billing.loading}
            onClick={() => void billingStore.refresh()}
          >
            <Icon name="refresh" size={15} />
            {billing.loading ? "正在刷新…" : "刷新额度"}
          </button>
        </div>
        <ErrorNotice error={billing.error} />
        {billing.error && data && (
          <p className="account-note" role="status">
            刷新失败，以下为上次读取的数据。
          </p>
        )}
        {!data && !billing.error && (
          <p role="status" className="account-note">
            正在读取你的额度…
          </p>
        )}
        {data && (
          <>
            <div className="account-metrics">
              <article aria-label="可用转写">
                <h3>
                  <Icon name="audio" size={17} />
                  可用转写
                </h3>
                <p className="account-metric-value">
                  {durationLabel(data.balances.asr_seconds.available)}
                </p>
                <p>
                  执行中占用 {durationLabel(data.balances.asr_seconds.held)}
                </p>
              </article>
              <article aria-label="可用 AI 生成">
                <h3>
                  <Icon name="sparkles" size={17} />
                  可用 AI 生成
                </h3>
                <p className="account-metric-value">
                  {data.balances.ai_generations.available}
                  <span>次</span>
                </p>
                <p>执行中占用 {data.balances.ai_generations.held} 次</p>
              </article>
              <article aria-label="今日下载">
                <h3>
                  <Icon name="download" size={17} />
                  今日下载
                </h3>
                <p
                  className={
                    data.member
                      ? "account-metric-value account-member-download"
                      : "account-metric-value"
                  }
                >
                  {data.member
                    ? "不按次数计费"
                    : `${data.daily.downloads_used} / ${catalog?.free_daily_downloads ?? "—"}`}
                  <span>{data.member ? "" : "次"}</span>
                </p>
                <p>
                  {data.member
                    ? `今日已用 ${data.daily.downloads_used} 次 · `
                    : ""}
                  执行中占用 {data.daily.downloads_held} 次
                </p>
                <p>
                  今日流量 {trafficLabel(data.daily.egress_used_bytes)} /{" "}
                  {trafficLabel(data.limits.daily_egress_bytes)}
                </p>
                {data.daily.egress_held_bytes > 0 && (
                  <p>
                    执行中占用流量 {trafficLabel(data.daily.egress_held_bytes)}
                  </p>
                )}
              </article>
            </div>
            {!data.member && catalogError && (
              <p className="account-note" role="status">
                每日下载次数上限暂时无法读取。
                <button
                  className="text-button"
                  onClick={() => setAttempt((value) => value + 1)}
                >
                  重新读取上限
                </button>
              </p>
            )}
            <p className="account-note">
              下载次数与流量于北京时间每天 00:00
              重置。任务执行中占用的额度暂不可用于新任务。
            </p>
            {!data.subscribed && (
              <p className="account-note">
                重新关注公众号后，可继续使用体验额度和免费新下载；有效的付费额度与已有结果仍可使用。
              </p>
            )}
            <details className="account-allowance-details">
              <summary>
                <span>
                  <Icon name="storage" size={17} />
                  额度明细
                </span>
                <span>
                  查看来源和有效期
                  <Icon name="plus" size={16} />
                </span>
              </summary>
              {currentAllowances.length ? (
                <ul>
                  {currentAllowances.map((item, index) => {
                    const available = Math.max(
                      0,
                      item.granted - item.used - item.held,
                    );
                    const pending =
                      new Date(item.starts_at).getTime() > Date.now();
                    const paused = item.source === "trial" && !data.subscribed;
                    return (
                      <li
                        key={`${item.kind}-${item.source}-${item.starts_at}-${index}`}
                      >
                        <strong>
                          {allowanceSourceLabel(item.source)} ·{" "}
                          {item.kind === "asr_seconds" ? "转写" : "AI"}额度
                        </strong>
                        <span>
                          {pending
                            ? "尚未生效"
                            : paused
                              ? "暂不可用 · 需关注公众号"
                              : `可用 ${item.kind === "asr_seconds" ? durationLabel(available) : `${available} 次`}`}
                        </span>
                        <span>
                          {pending
                            ? `${dateLabel(item.starts_at)} 生效 · `
                            : ""}
                          {dateLabel(item.expires_at)} 到期
                        </span>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p className="account-note">
                  暂无有效额度。你可以查看订阅方案，了解可用权益。
                </p>
              )}
            </details>
          </>
        )}
      </section>

      <section
        className="account-information"
        aria-labelledby="account-information-title"
      >
        <h2 id="account-information-title">账户信息</h2>
        <div className="account-login-method">
          <span>
            <Icon name="shield" size={18} />
            微信登录
          </span>
          <span>已连接 · 个人记录仅自己可见</span>
        </div>
        <nav className="account-shortcuts" aria-label="个人中心常用入口">
          <Link to="/library">
            <Icon name="bookmark" size={17} />
            我的知识库
            <Icon name="arrow" size={15} />
          </Link>
          <button
            onClick={() => {
              if (authStore.requireLogin()) store.setHistoryOpen(true);
            }}
          >
            <Icon name="history" size={17} />
            任务历史
            <Icon name="arrow" size={15} />
          </button>
          <Link to="/settings">
            <Icon name="menu" size={17} />
            设置
            <Icon name="arrow" size={15} />
          </Link>
        </nav>
      </section>
    </>
  );
}
