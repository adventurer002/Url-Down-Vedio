/** Approved public proposal. These values do not grant or change live entitlements. */
export const billingPeriods = [
  { id: "monthly", label: "月付", unit: "月", months: 1 },
  { id: "quarterly", label: "季付", unit: "季", months: 3 },
  { id: "annual", label: "年付", unit: "年", months: 12 },
] as const;
export type BillingPeriod = (typeof billingPeriods)[number]["id"];
export const subscriptionOffers = [
  {
    id: "plus",
    name: "Plus",
    description: "适合轻量学习与日常记录",
    prices: { monthly: 990, quarterly: 2690, annual: 9900 },
    monthlyMinutes: 150,
    monthlyGenerations: 50,
    benefits: [
      "不限次数视频解析",
      "每日 10 GB 下载流量",
      "单视频最长 60 分钟",
      "总结 + 思维导图，组合仅扣 1 次",
    ],
  },
  {
    id: "pro",
    name: "Pro",
    description: "适合课程、访谈与持续研究",
    prices: { monthly: 1690, quarterly: 4590, annual: 16900 },
    monthlyMinutes: 300,
    monthlyGenerations: 100,
    benefits: [
      "不限次数视频解析",
      "每日下载流量不限量",
      "单视频最长 120 分钟",
      "总结 + 思维导图，组合仅扣 1 次",
    ],
  },
] as const;
export const addonOffers = [
  {
    id: "asr_120",
    name: "转写额度包",
    detail: "120 分钟 · 90 天有效",
    priceFen: 990,
    icon: "audio",
  },
  {
    id: "ai_50",
    name: "AI 额度包",
    detail: "50 次 · 90 天有效",
    priceFen: 590,
    icon: "sparkles",
  },
] as const;
export const offerPrice = (fen: number) =>
  (fen / 100)
    .toFixed(2)
    .replace(/\.00$/, "")
    .replace(/(\.\d)0$/, "$1");

/** External checkout links supplied by the site owner; no automatic entitlement grant. */
export const paymentLinks = {
  plus: {
    monthly: "https://vn4mfnqg.jsjform.com/p/o6p8tx",
    quarterly: "https://vn4mfnqg.jsjform.com/p/OtQYmf",
    annual: "https://vn4mfnqg.jsjform.com/p/ANUdVP",
  },
  pro: {
    monthly: "https://vn4mfnqg.jsjform.com/p/MDriLx",
    quarterly: "https://vn4mfnqg.jsjform.com/p/tO1aqa",
    annual: "https://vn4mfnqg.jsjform.com/p/cCB5jS",
  },
} as const satisfies Record<"plus" | "pro", Record<BillingPeriod, string>>;
