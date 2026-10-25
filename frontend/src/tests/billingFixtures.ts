import type {
  AllowanceQuote,
  Allowances,
  BillingCatalog,
} from "../types/billing";
export const limits = {
  video_seconds: 1200,
  file_bytes: 268435456,
  daily_egress_bytes: 1073741824,
  download_concurrency: 1,
};
export const catalog: BillingCatalog = {
  version: "allowances-v1",
  currency: "CNY",
  products: {
    monthly: {
      name: "月会员",
      price_fen: 1990,
      asr_seconds: 18000,
      ai_generations: 100,
    },
    asr_120: {
      name: "120 分钟转写包",
      price_fen: 990,
      asr_seconds: 7200,
      ai_generations: 0,
    },
    ai_100: {
      name: "100 次 AI 包",
      price_fen: 990,
      asr_seconds: 0,
      ai_generations: 100,
    },
  },
  trial: { asr_seconds: 1800, ai_generations: 5, valid_days: 30 },
  addon_valid_days: 30,
  rollover: false,
  free_daily_downloads: 2,
  free_limits: limits,
  member_limits: {
    video_seconds: 3600,
    file_bytes: 536870912,
    daily_egress_bytes: 5368709120,
    download_concurrency: 3,
  },
  real_payment_enabled: false,
  refund_policy: "正常交付后不支持无理由退款",
  max_text_chars: 60000,
  max_output_tokens_per_call: 4096,
  max_total_output_tokens: 16384,
  max_llm_calls: 16,
};
export const allowances: Allowances = {
  version: "allowances-v1",
  member: false,
  member_until: null,
  subscribed: true,
  balances: {
    asr_seconds: { available: 1799, held: 1 },
    ai_generations: { available: 5, held: 0 },
  },
  limits,
  daily: {
    date: "2026-10-24",
    downloads_used: 1,
    downloads_held: 0,
    egress_used_bytes: 1024,
    egress_held_bytes: 0,
  },
  allowances: [
    {
      kind: "asr_seconds",
      source: "trial",
      granted: 1800,
      used: 0,
      held: 1,
      starts_at: "2026-10-01T00:00:00Z",
      expires_at: "2099-01-01T00:00:00Z",
      expired: false,
    },
  ],
};
export const quote: AllowanceQuote = {
  quote_id: "quote-1",
  task_type: "download",
  maximum: {
    version: "allowances-v1",
    asr_seconds: 0,
    ai_generations: 0,
    limits,
  },
  expires_at: "2099-01-01T00:00:00Z",
  note: "执行前确认",
};
