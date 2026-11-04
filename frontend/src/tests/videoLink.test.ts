import { describe, expect, it } from "vitest";
import { extractVideoLink } from "../utils/videoLink";

describe("share link extraction", () => {
  it.each([
    [
      "https://www.bilibili.com/video/BVabc?p=2",
      "https://www.bilibili.com/video/BVabc?p=2",
    ],
    [
      "复制后打开 https://v.douyin.com/abcd/， 看看这个视频",
      "https://v.douyin.com/abcd/",
    ],
    ["分享\nhttps://youtu.be/dQw4w9WgXcQ\n", "https://youtu.be/dQw4w9WgXcQ"],
  ])("keeps exactly one URL in %s", (input, expected) => {
    expect(extractVideoLink(input)).toBe(expected);
  });
  it.each([
    "没有网址",
    "https://b23.tv/a https://v.douyin.com/b",
    "ftp://example.com/a",
  ])("rejects invalid or ambiguous input %s", (input) => {
    expect(() => extractVideoLink(input)).toThrow();
  });
});
