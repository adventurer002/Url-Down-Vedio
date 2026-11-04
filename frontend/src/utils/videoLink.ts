import { ApiError } from "../api/client";

export function extractVideoLink(value: string): string {
  const text = value.trim();
  const matches = text.match(/https?:\/\/[^\s<>"\u3000]+/gi) || [];
  if (matches.length !== 1)
    throw new ApiError(
      "INVALID_URL",
      "请输入一个完整的视频链接，分享文本只能包含一个链接。",
    );
  const url = matches[0]!.replace(/[，。；！、）】》]+$/u, "");
  try {
    const parsed = new URL(url);
    if (!["http:", "https:"].includes(parsed.protocol)) throw new Error();
  } catch {
    throw new ApiError("INVALID_URL", "请输入完整的 http 或 https 视频链接。");
  }
  return url;
}
