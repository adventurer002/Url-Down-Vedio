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
