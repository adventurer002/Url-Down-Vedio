import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../stores/auth";
import Icon from "../components/Icon";
import {
  addonOffers,
  billingPeriods,
  offerPrice,
  subscriptionOffers,
  type BillingPeriod,
} from "../subscriptionOffers";

type Selection = {
  name: string;
  priceFen: number;
  detail: string;
};
function PurchaseNotice({
  selection,
  onClose,
}: {
  selection: Selection;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = ref.current;
    dialog?.showModal();
    return () => {
      dialog?.close();
      previous?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className="billing-dialog pricing-dialog"
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
      <span className="pricing-coming">新方案待开放</span>
      <h2 id="purchase-title">{selection.name}</h2>
      <p className="purchase-price pricing-dialog-price">
        ¥{offerPrice(selection.priceFen)}
      </p>
      <p>{selection.detail}</p>
      <p>
        暂未开放在线购买。支付与权益发放接通后再开放购买，当前展示为新方案预告，尚未生效；已获得的会员与额度仍按原规则使用，可在个人中心查看。
      </p>
      <button
        className="pricing-button pricing-button-primary"
        onClick={onClose}
      >
        我知道了
      </button>
    </dialog>
  );
}
function Benefits({ items }: { items: readonly string[] }) {
  return (
    <ul className="pricing-benefits">
      {items.map((item) => (
        <li key={item}>
          <Icon size={17} />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}
export default function SubscriptionView() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [period, setPeriod] = useState<BillingPeriod>("monthly");
  const [selection, setSelection] = useState<Selection>();
  const selectedPeriod = billingPeriods.find((item) => item.id === period)!;
  return (
    <div className="subscription-page pricing-page">
      <nav className="pricing-breadcrumb" aria-label="面包屑">
        <a href="/">首页</a>
        <span>/</span>
        <span>订阅方案</span>
      </nav>
      <section className="pricing-heading">
        <h1>选择适合你的节奏。</h1>
        <p>从免费体验，到持续积累。找到适合你的使用方式。</p>
        <div className="pricing-periods" role="group" aria-label="付费周期">
          {billingPeriods.map((item) => (
            <button
              key={item.id}
              aria-pressed={period === item.id}
              onClick={() => setPeriod(item.id)}
            >
              {item.label}
              {item.id === "annual" && <span>赠送 2 个月</span>}
            </button>
          ))}
        </div>
      </section>
      <section className="pricing-grid" aria-label="选择套餐">
        <article
          className="pricing-card pricing-free"
          aria-labelledby="free-plan-title"
        >
          <h2 id="free-plan-title">免费体验</h2>
          <p className="pricing-description">先体验，再决定</p>
          <p className="pricing-price">
            <strong>¥0</strong>
            <span>/ 免费</span>
          </p>
          <p className="pricing-price-detail">无需购买会员</p>
          <div className="pricing-free-rights">
            <Benefits items={["每日 2 次视频解析", "单视频最长 20 分钟"]} />
            <p>转写与 AI 额度可另购</p>
          </div>
          <button
            className="pricing-button"
            onClick={() => navigate(user ? "/account" : "/app")}
          >
            {user ? "查看我的体验额度" : "开始体验"}
          </button>
        </article>
        {subscriptionOffers.map((offer) => (
          <article
            key={offer.id}
            className={`pricing-card pricing-${offer.id}`}
            aria-labelledby={`${offer.id}-plan-title`}
          >
            {offer.id === "pro" && (
              <span className="pricing-recommended">推荐</span>
            )}
            <h2 id={`${offer.id}-plan-title`}>{offer.name}</h2>
            <p className="pricing-description">{offer.description}</p>
            <p className="pricing-price">
              <strong>¥{offerPrice(offer.prices[period])}</strong>
              <span>/ {selectedPeriod.unit}</span>
            </p>
            <p className="pricing-price-detail">
              {period === "monthly"
                ? `季付 ¥${offerPrice(offer.prices.quarterly)} / 季 · 年付 ¥${offerPrice(offer.prices.annual)} / 年`
                : `一次支付 ¥${offerPrice(offer.prices[period])}，折合 ¥${(offer.prices[period] / 100 / selectedPeriod.months).toFixed(2)} / 月`}
            </p>
            <div
              className="pricing-quotas"
              aria-label={`${offer.name} 每月额度`}
            >
              <div>
                <Icon name="audio" size={25} />
                <p>
                  <strong>{offer.monthlyMinutes} 分钟 / 月</strong>
                  <span>语音转写</span>
                </p>
              </div>
              <div>
                <Icon name="sparkles" size={25} />
                <p>
                  <strong>{offer.monthlyGenerations} 次 / 月</strong>
                  <span>AI 生成</span>
                </p>
              </div>
            </div>
            <Benefits items={offer.benefits} />
            <p className="pricing-upgrade">
              {offer.id === "pro"
                ? `每${selectedPeriod.unit}多 ¥${offerPrice(offer.prices[period] - subscriptionOffers[0].prices[period])}，转写与 AI 额度翻倍`
                : "核心功能，两档都拥有"}
            </p>
            <button
              className={`pricing-button${offer.id === "pro" ? " pricing-button-primary" : ""}`}
              onClick={() =>
                setSelection({
                  name: `${offer.name} · ${selectedPeriod.label}`,
                  priceFen: offer.prices[period],
                  detail: `一次支付，服务期 ${selectedPeriod.months} 个月。每月 ${offer.monthlyMinutes} 分钟转写、${offer.monthlyGenerations} 次 AI，额度按月发放，不结转。`,
                })
              }
            >
              选择 {offer.name}
            </button>
          </article>
        ))}
      </section>
      <p className="pricing-renewal">
        会员额度每月发放，当月有效，不结转。一次购买，到期手动续费。
      </p>
      <p className="pricing-availability" role="status">
        新方案待开放：月付、季付、年付与额度包暂未开放购买。已有权益仍按原规则使用，
        <Link to="/account">查看我的实际额度</Link>。
      </p>
      <section className="pricing-addons" aria-labelledby="addons-title">
        <h2 id="addons-title">不够用？按需加一点。</h2>
        <p>额度包可单独购买，无需先开会员。</p>
        <div className="pricing-addon-grid">
          {addonOffers.map((addon) => (
            <article key={addon.id} className="pricing-addon-card">
              <Icon name={addon.icon} size={28} />
              <div>
                <h3>{addon.name}</h3>
                <p>{addon.detail}</p>
              </div>
              <strong>¥{offerPrice(addon.priceFen)}</strong>
              <button
                className="pricing-button"
                aria-label={`购买${addon.name}`}
                onClick={() =>
                  setSelection({
                    name: addon.name,
                    priceFen: addon.priceFen,
                    detail: addon.detail,
                  })
                }
              >
                购买额度包
              </button>
            </article>
          ))}
        </div>
      </section>
      <section className="pricing-rules" aria-labelledby="rules-title">
        <h2 id="rules-title">用得明白，买得放心</h2>
        <div>
          {[
            ["成功后扣额度", "未成功交付，不核销额度"],
            ["组合生成只扣 1 次", "总结与思维导图一起生成，合计扣 1 次"],
            ["已有结果免费查看", "查看、复制和导出不重复扣费"],
          ].map(([title, text]) => (
            <article key={title}>
              <Icon name="shield" size={21} />
              <div>
                <h3>{title}</h3>
                <p>{text}</p>
              </div>
            </article>
          ))}
        </div>
      </section>
      <section className="pricing-faq" aria-label="订阅常见问题">
        <details>
          <summary>
            转写分钟和 AI 次数如何计算？
            <Icon name="plus" size={16} />
          </summary>
          <p>
            语音转写按整段音频实际时长，以秒向上取整；已有字幕和转写缓存不扣分钟。总结与思维导图组合整体成功仅扣
            1 次。每个新问题成功回答扣 1 次 AI，平台内部拆分和重试不另扣次数。
          </p>
        </details>
        <details>
          <summary>
            季付和年付的额度如何发放？
            <Icon name="plus" size={16} />
          </summary>
          <p>
            一次支付所选周期的总价。从开通日起，每个订阅月发放当月额度；当月未用完不结转，不一次性发放整个季度或全年的额度。到期手动续费，不自动扣款。
          </p>
        </details>
        <details>
          <summary>
            额度包需要先开会员吗？
            <Icon name="plus" size={16} />
          </summary>
          <p>
            不需要。新方案中的额度包购买后 90
            天有效，与会员独立计算；优先消耗即将到期的额度。购买额度包不附带会员的解析、时长或下载权益。
          </p>
        </details>
      </section>
      {selection && (
        <PurchaseNotice
          selection={selection}
          onClose={() => setSelection(undefined)}
        />
      )}
    </div>
  );
}
