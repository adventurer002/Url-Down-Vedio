import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { normalizeError, type ApiError } from "../api/client";
import { authStore, useAuth } from "../stores/auth";
import { billingStore, useBilling } from "../stores/billing";
import type { BillingCatalog, Product } from "../types/billing";
import { dateLabel, durationLabel, priceLabel } from "../utils/billing";
import ErrorNotice from "../components/ErrorNotice";
import Icon from "../components/Icon";
import PixelCard from "../components/PixelCard";

function Benefits({ items }: { items: string[] }) {
  return (
    <ul className="plan-benefits">
      {items.map((item) => (
        <li key={item}>
          <Icon size={15} />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}

function PurchaseNotice({
  product,
  onClose,
}: {
  product: Product;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.showModal();
    return () => {
      ref.current?.close();
      previous?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className="billing-dialog purchase-dialog"
      aria-labelledby="purchase-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <button
        className="dialog-close"
        aria-label="关闭开通说明"
        onClick={onClose}
      >
        <Icon name="close" />
      </button>
      <span className="billing-dialog-icon">
        <Icon name="sparkles" size={26} />
      </span>
      <p className="eyebrow">A LITTLE MORE POSSIBILITY</p>
      <h2 id="purchase-title">{product.name}</h2>
      <p className="purchase-price">¥{priceLabel(product.price_fen)}</p>
      <p>
        暂未开放在线购买。你可以先使用免费体验额度，已获得的权益会在账户中显示。
      </p>
      <button className="button primary full" onClick={onClose}>
        我知道了
      </button>
    </dialog>
  );
}

export default function SubscriptionView() {
  const { user } = useAuth();
  const billing = useBilling();
  const [catalog, setCatalog] = useState<BillingCatalog>();
  const [error, setError] = useState<ApiError | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [addon, setAddon] = useState<"asr_120" | "ai_100">("asr_120");
  const [purchase, setPurchase] = useState<Product>();
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    void api
      .catalog(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setCatalog(value);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(normalizeError(cause));
      });
    return () => controller.abort();
  }, [attempt]);
  const member = billing.data?.member;
  const trial = catalog?.trial;
  const monthly = catalog?.products.monthly;
  const extra = catalog?.products[addon];
  const cardProps = {
    theme: "light" as const,
    colors: ["#18181b", "#cfd8e3"],
    glowColor: "#71717a",
    shape: "square" as const,
    origin: "center" as const,
    gap: 5,
    pixelSize: 2,
    speed: 35,
    revealDuration: 600,
    spotlight: 0.5,
    glow: 0.5,
    opacity: 0.35,
    fade: 0.7,
    trigger: "hover" as const,
    radius: 24,
    clickPulse: true,
    intro: true,
  };

  return (
    <div className="subscription-page">
      <div className="subscription-breadcrumb">
        <Link to="/">首页</Link>
        <span>/</span>
        <span>订阅与额度</span>
      </div>
      <section className="subscription-heading">
        <span className="membership-tag">
          <span /> 为每一次好奇，多留一点空间
        </span>
        <h1>
          好内容，<span>值得更进一步。</span>
        </h1>
        <p>
          从一段视频，到真正属于你的知识。
          <br className="mobile-break" />
          选择适合自己的节奏，其余交给 AI。
        </p>
        <div className="subscription-assurances">
          <span>
            <Icon size={14} /> 下载与 AI 额度独立
          </span>
          <span>
            <Icon size={14} /> 失败不扣额度
          </span>
          <span>
            <Icon size={14} /> 已有结果随时查看
          </span>
        </div>
      </section>

      <ErrorNotice error={error}>
        <button
          className="text-button"
          onClick={() => setAttempt((value) => value + 1)}
        >
          重新加载套餐
        </button>
      </ErrorNotice>
      {!catalog && !error && (
        <div className="subscription-loading" role="status">
          <span className="spinner" /> 正在加载套餐…
        </div>
      )}
      {catalog && trial && monthly && extra && (
        <>
          <section className="subscription-plans" aria-label="选择套餐">
            <PixelCard
              {...cardProps}
              className="subscription-card"
              introDelay={0}
            >
              <div className="plan-content">
                <div className="plan-heading">
                  <span className="plan-icon">
                    <Icon name="seedling" size={22} />
                  </span>
                  <span className="plan-label">轻松开始</span>
                </div>
                <h2>免费体验</h2>
                <p className="plan-description">先发现，一段视频的更多可能。</p>
                <div className="plan-price">
                  <span>¥</span>
                  <strong>0</strong>
                  <small>/ 一次领取</small>
                </div>
                <p className="plan-price-note">关注公众号并绑定账号后获得</p>
                <button
                  className="button secondary full plan-action"
                  onClick={() => {
                    if (!user) authStore.open();
                    else
                      document.getElementById("my-allowances")?.scrollIntoView({
                        behavior: "smooth",
                        block: "center",
                      });
                  }}
                >
                  {user ? "查看我的体验额度" : "免费开始体验"}
                  <Icon name="arrow" size={16} />
                </button>
                <div className="plan-divider" />
                <Benefits
                  items={[
                    `${durationLabel(trial.asr_seconds)}语音转写`,
                    `${trial.ai_generations} 次 AI 生成`,
                    `每天 ${catalog.free_daily_downloads} 次成功下载`,
                    `单视频最长 ${catalog.free_limits.video_seconds / 60} 分钟`,
                    "总结与导图再次查看免费",
                  ]}
                />
                <p className="plan-validity">
                  体验额度领取后 {trial.valid_days} 天有效 · 每个账号限领一次
                </p>
              </div>
            </PixelCard>

            <PixelCard
              {...cardProps}
              className="subscription-card featured-plan"
              introDelay={120}
            >
              <div className="plan-content">
                <div className="plan-heading">
                  <span className="plan-icon">
                    <Icon name="sparkles" size={23} />
                  </span>
                  <span className="plan-label">持续探索</span>
                  <span className="plan-recommended">推荐</span>
                </div>
                <h2>月度会员</h2>
                <p className="plan-description">
                  把日常的观看，变成持续的积累。
                </p>
                <div className="plan-price">
                  <span>¥</span>
                  <strong>{priceLabel(monthly.price_fen)}</strong>
                  <small>/ 月</small>
                </div>
                <p className="plan-price-note">按开通日期计算账期 · 手动续费</p>
                <button
                  className="button primary full plan-action"
                  onClick={() => setPurchase(monthly)}
                >
                  {member ? "续费说明" : "开通说明"}
                  <Icon name="arrow" size={16} />
                </button>
                <div className="plan-divider" />
                <Benefits
                  items={[
                    `每账期 ${durationLabel(monthly.asr_seconds)}转写`,
                    `每账期 ${monthly.ai_generations} 次 AI 生成`,
                    "视频下载不按次数计费",
                    `单视频最长 ${catalog.member_limits.video_seconds / 60} 分钟`,
                    `每日 ${catalog.member_limits.daily_egress_bytes / 1024 ** 3} GiB 下载流量`,
                    "总结与导图组合仅扣 1 次",
                  ]}
                />
                <p className="plan-validity">
                  月度额度到期刷新，不结转 · 不自动扣款
                </p>
              </div>
            </PixelCard>

            <PixelCard
              {...cardProps}
              className="subscription-card"
              introDelay={240}
            >
              <div className="plan-content">
                <div className="plan-heading">
                  <span className="plan-icon">
                    <Icon name="plus" size={23} />
                  </span>
                  <span className="plan-label">按需补充</span>
                </div>
                <h2>灵活加购</h2>
                <p className="plan-description">灵感正盛时，不必停在这里。</p>
                <div className="plan-price">
                  <span>¥</span>
                  <strong>{priceLabel(extra.price_fen)}</strong>
                  <small>/ 包</small>
                </div>
                <div
                  className="addon-switch"
                  role="group"
                  aria-label="选择加购额度"
                >
                  <button
                    aria-pressed={addon === "asr_120"}
                    onClick={() => setAddon("asr_120")}
                  >
                    转写分钟包
                  </button>
                  <button
                    aria-pressed={addon === "ai_100"}
                    onClick={() => setAddon("ai_100")}
                  >
                    AI 次数包
                  </button>
                </div>
                <button
                  className="button secondary full plan-action"
                  onClick={() => setPurchase(extra)}
                >
                  加购说明
                  <Icon name="arrow" size={16} />
                </button>
                <div className="plan-divider" />
                <Benefits
                  items={[
                    addon === "asr_120"
                      ? `${durationLabel(extra.asr_seconds)}语音转写`
                      : `${extra.ai_generations} 次 AI 生成`,
                    "无需开通会员也可使用",
                    "与会员额度独立，到期时间不变",
                    "失败或有效取消自动释放额度",
                    "操作前确认本次最高消耗",
                  ]}
                />
                <p className="plan-validity">
                  发放后 {catalog.addon_valid_days} 天有效 ·
                  会员到期不影响有效加购包
                </p>
              </div>
            </PixelCard>
          </section>
          <p className="subscription-sale-note">
            暂未开放在线购买。你可以先领取体验额度，已获得的权益可在下方查看。
          </p>
        </>
      )}

      <section
        className="allowance-overview"
        id="my-allowances"
        aria-labelledby="allowance-title"
      >
        <div className="allowance-section-heading">
          <div>
            <span className="eyebrow">YOUR SPACE</span>
            <h2 id="allowance-title">我的额度</h2>
          </div>
          {user && (
            <button
              className="text-button"
              disabled={billing.loading}
              onClick={() => void billingStore.refresh()}
            >
              <Icon name="refresh" size={15} />
              {billing.loading ? "正在刷新…" : "刷新额度"}
            </button>
          )}
        </div>
        {!user ? (
          <div className="allowance-guest">
            <p>登录后，查看属于你的转写与 AI 额度。</p>
            <button className="button secondary" onClick={authStore.open}>
              登录查看
              <Icon name="arrow" size={15} />
            </button>
          </div>
        ) : (
          <>
            <ErrorNotice error={billing.error} />
            {!billing.data && billing.loading && (
              <p className="muted" role="status">
                正在读取你的额度…
              </p>
            )}
            {billing.data && (
              <>
                <div className="allowance-metrics">
                  <div className="allowance-metric">
                    <span>当前身份</span>
                    <strong>{member ? "月度会员" : "免费用户"}</strong>
                    <small>
                      {member
                        ? `有效至 ${dateLabel(billing.data.member_until)}`
                        : "按自己的节奏，开始探索"}
                    </small>
                  </div>
                  <div className="allowance-metric">
                    <span>剩余转写额度</span>
                    <strong>
                      {durationLabel(
                        billing.data.balances.asr_seconds.available,
                      )}
                    </strong>
                    <small>
                      执行中占用{" "}
                      {durationLabel(billing.data.balances.asr_seconds.held)}
                    </small>
                  </div>
                  <div className="allowance-metric">
                    <span>剩余 AI 生成</span>
                    <strong>
                      {billing.data.balances.ai_generations.available}
                      <em> 次</em>
                    </strong>
                    <small>
                      执行中占用 {billing.data.balances.ai_generations.held} 次
                    </small>
                  </div>
                </div>
                <div className="allowance-footnote">
                  <span>
                    {member
                      ? "今日下载不按次数计费"
                      : `今日下载：已用 ${billing.data.daily.downloads_used} 次 / 占用 ${billing.data.daily.downloads_held} 次`}
                  </span>
                  <span>
                    今日已用流量{" "}
                    {(billing.data.daily.egress_used_bytes / 1024 ** 2).toFixed(
                      1,
                    )}{" "}
                    MiB
                  </span>
                </div>
                {!billing.data.subscribed && (
                  <p className="allowance-subscribe-note">
                    重新关注公众号后，可继续使用体验额度和免费新下载；有效的付费额度与已有结果仍可使用。
                  </p>
                )}
                {billing.data.allowances.filter(
                  (item) =>
                    !item.expired &&
                    new Date(item.starts_at).getTime() <= Date.now(),
                ).length > 0 && (
                  <details className="allowance-expiry">
                    <summary>查看额度有效期</summary>
                    <ul>
                      {billing.data.allowances
                        .filter(
                          (item) =>
                            !item.expired &&
                            new Date(item.starts_at).getTime() <= Date.now(),
                        )
                        .map((item, index) => (
                          <li key={`${item.kind}-${item.source}-${index}`}>
                            <span>
                              {item.source === "trial"
                                ? "体验"
                                : item.source === "monthly"
                                  ? "会员"
                                  : "加购"}
                              {item.kind === "asr_seconds" ? "转写" : "AI"}额度
                            </span>
                            <span>{dateLabel(item.expires_at)} 到期</span>
                          </li>
                        ))}
                    </ul>
                  </details>
                )}
              </>
            )}
          </>
        )}
      </section>

      <section className="subscription-faq" aria-label="订阅常见问题">
        <div>
          <span className="eyebrow">GOOD TO KNOW</span>
          <h2>
            简单的规则，
            <br />
            安心地使用。
          </h2>
          <p>每一次消耗，都在开始前说清楚。</p>
        </div>
        <div className="faq-list">
          <details>
            <summary>
              分钟和 AI 次数分别怎么计算？
              <Icon name="plus" size={16} />
            </summary>
            <p>
              语音转写按整段音频的实际时长，以秒向上取整；已有字幕和转写缓存不扣分钟。总结消耗
              1 次，总结与导图组合整体成功也只扣 1 次。视频问答每个新问题成功扣
              1 次
              AI，不重复扣转写分钟；相同文稿、模型和版本的已有回答免费，无足够原文依据时不扣次数。
            </p>
            {catalog && (
              <p>
                单次生成支持最多 {catalog.max_text_chars}{" "}
                字符文稿；每次模型调用最多输出{" "}
                {catalog.max_output_tokens_per_call} Token， 整次操作最多{" "}
                {catalog.max_llm_calls} 次调用、累计{" "}
                {catalog.max_total_output_tokens} 输出
                Token。平台内部拆分和重试不另扣次数。
              </p>
            )}
          </details>
          <details>
            <summary>
              失败、取消或再次查看，会扣额度吗？
              <Icon name="plus" size={16} />
            </summary>
            <p>
              任务开始执行前占用最高额度，成功后核销；失败或有效取消释放占用。自己的已有结果、导图交互和导出免费，公共
              AI 缓存首次获取计 1 次。
            </p>
          </details>
          <details>
            <summary>
              免费与会员下载有哪些限制？
              <Icon name="plus" size={16} />
            </summary>
            <p>
              会员下载不按次数收费。单视频最长{" "}
              {catalog ? catalog.member_limits.video_seconds / 60 : "—"}{" "}
              分钟，单文件最多{" "}
              {catalog ? catalog.member_limits.file_bytes / 1024 ** 2 : "—"}{" "}
              MiB，每日流量{" "}
              {catalog
                ? catalog.member_limits.daily_egress_bytes / 1024 ** 3
                : "—"}{" "}
              GiB。任务按服务负载排队，文件保留 24 小时。
            </p>
            {catalog && (
              <p>
                免费用户每天成功下载 {catalog.free_daily_downloads} 次，北京时间
                00:00 重置、不累计； 视频最长{" "}
                {catalog.free_limits.video_seconds / 60} 分钟，文件最多{" "}
                {catalog.free_limits.file_bytes / 1024 ** 2} MiB， 每日流量{" "}
                {catalog.free_limits.daily_egress_bytes / 1024 ** 3}{" "}
                GiB。重复取回文件也计入流量。 免费 / 会员账号最多分别占用{" "}
                {catalog.free_limits.download_concurrency} /{" "}
                {catalog.member_limits.download_concurrency}{" "}
                个下载执行名额，实际执行按服务负载排队。
              </p>
            )}
          </details>
          <details>
            <summary>
              额度会结转，或自动续费吗？
              <Icon name="plus" size={16} />
            </summary>
            <p>
              月度额度按账期刷新，不结转，不自动扣款。加购包独立计算有效期，不受会员到期影响。正常交付后不支持无理由退款，支付异常及依法应退款的情形除外。
            </p>
          </details>
        </div>
      </section>
      <Link className="subscription-return" to="/">
        回到工作台，开始一次新的探索
        <Icon name="arrow" size={17} />
      </Link>
      {purchase && (
        <PurchaseNotice
          product={purchase}
          onClose={() => setPurchase(undefined)}
        />
      )}
    </div>
  );
}
