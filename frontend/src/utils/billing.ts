import type { Allowances, MembershipTier } from "../types/billing";

export function allowanceSourceLabel(source: string) {
  return (
    (
      {
        trial: "体验",
        monthly: "旧版月会员",
        plus_monthly: "Plus 月会员",
        pro_monthly: "Pro 月会员",
        asr_120: "120 分钟转写包",
        ai_100: "100 次 AI 包",
        addon: "加购",
      } as Record<string, string>
    )[source] ?? "其他"
  );
}

/** Refresh at the next entitlement boundary or Beijing midnight, at least once a minute. */
export function billingFreshUntil(data: Allowances, now: number) {
  const day = 86_400_000;
  const offset = 8 * 3_600_000;
  const midnight = (Math.floor((now + offset) / day) + 1) * day - offset;
  const boundaries = [
    data.member_until,
    ...data.allowances.flatMap((item) => [item.starts_at, item.expires_at]),
  ]
    .filter((value): value is string => !!value)
    .map((value) => new Date(value).getTime())
    .filter((value) => value > now);
  return boundaries.reduce(
    (earliest, value) => Math.min(earliest, value),
    Math.min(now + 60_000, midnight),
  );
}

export function durationLabel(seconds: number) {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder
    ? `${minutes ? `${minutes} 分 ` : ""}${remainder} 秒`
    : `${minutes} 分钟`;
}
export function priceLabel(fen: number) {
  return (fen / 100).toFixed(2);
}
export function dateLabel(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date(value));
}

export function membershipLabel(
  tier: MembershipTier | undefined,
  member: boolean,
) {
  if (!member) return "免费用户";
  return {
    free: "免费用户",
    legacy: "旧版会员",
    plus: "Plus 会员",
    pro: "Pro 会员",
  }[tier || "legacy"];
}
