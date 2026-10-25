import type { JobKind } from "./api";

export interface Product {
  name: string;
  price_fen: number;
  asr_seconds: number;
  ai_generations: number;
}
export interface ResourceLimits {
  video_seconds: number;
  file_bytes: number;
  daily_egress_bytes: number;
  download_concurrency: number;
}
export interface BillingCatalog {
  version: string;
  currency: string;
  products: Record<"monthly" | "asr_120" | "ai_100", Product>;
  trial: { asr_seconds: number; ai_generations: number; valid_days: number };
  addon_valid_days: number;
  rollover: boolean;
  free_daily_downloads: number;
  free_limits: ResourceLimits;
  member_limits: ResourceLimits;
  real_payment_enabled: boolean;
  refund_policy: string;
  max_text_chars: number;
  max_output_tokens_per_call: number;
  max_total_output_tokens: number;
  max_llm_calls: number;
}
export interface Allowances {
  version: string;
  member: boolean;
  member_until: string | null;
  subscribed: boolean;
  balances: Record<
    "asr_seconds" | "ai_generations",
    { available: number; held: number }
  >;
  limits: ResourceLimits;
  daily: {
    date: string;
    downloads_used: number;
    downloads_held: number;
    egress_used_bytes: number;
    egress_held_bytes: number;
  };
  allowances: Array<{
    kind: string;
    source: string;
    granted: number;
    used: number;
    held: number;
    starts_at: string;
    expires_at: string;
    expired: boolean;
  }>;
}
export interface AllowanceQuote {
  quote_id: string;
  task_type: JobKind;
  maximum: {
    version: string;
    asr_seconds: number;
    ai_generations: number;
    limits: ResourceLimits;
  };
  expires_at: string;
  note: string;
}
