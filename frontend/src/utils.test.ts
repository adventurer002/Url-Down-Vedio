import { describe, expect, it } from "vitest";

import { formatBytes, formatDuration } from "./api/client";
import { isTerminal } from "./hooks/useVideos";

describe("formatDuration", () => {
  it("formats seconds as m:ss", () => {
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(65)).toBe("1:05");
    expect(formatDuration(null)).toBe("--:--");
  });
});

describe("formatBytes", () => {
  it("picks human units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(null)).toBe("--");
  });
});

describe("isTerminal", () => {
  it("matches backend terminal states", () => {
    expect(isTerminal("completed")).toBe(true);
    expect(isTerminal("failed")).toBe(true);
    expect(isTerminal("cancelled")).toBe(true);
    expect(isTerminal("downloading")).toBe(false);
    expect(isTerminal("uploading")).toBe(false);
  });
});
