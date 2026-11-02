// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import ErrorNotice from "../components/ErrorNotice";
import { ApiError } from "../api/client";
import TranscriptReader from "../components/TranscriptReader";
import MobileNavigation from "../components/MobileNavigation";
import PageErrorBoundary from "../components/PageErrorBoundary";
import { downloadText, transcriptToSrt } from "../utils";
import type { TranscriptResult } from "../types/api";

vi.mock("../utils", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  downloadText: vi.fn(),
}));

const transcript = {
  id: "long-transcript",
  source_type: "asr",
  language: "zh",
  segments: Array.from({ length: 250 }, (_, index) => ({
    start: index * 10,
    end: index * 10 + 9,
    text: `文稿第 ${index} 段内容`,
  })),
} as TranscriptResult;
beforeEach(() => {
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

it("paginates long transcripts and searches all segments beyond the current page", () => {
  render(
    <TranscriptReader
      transcript={transcript}
      sourceUrl="https://youtu.be/example"
    />,
  );
  expect(document.querySelectorAll(".transcript-segments > div")).toHaveLength(
    100,
  );
  expect(screen.queryByText("文稿第 220 段内容")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "下一页文稿" }));
  expect(screen.getByText("文稿第 100 段内容")).toBeTruthy();
  fireEvent.change(screen.getByRole("searchbox", { name: "搜索文稿" }), {
    target: { value: "第 220 段" },
  });
  expect(screen.getByText("文稿第 220 段内容")).toBeTruthy();
  expect(document.querySelectorAll(".transcript-segments > div")).toHaveLength(
    1,
  );
  fireEvent.change(screen.getByRole("searchbox", { name: "搜索文稿" }), {
    target: { value: "不存在的句子" },
  });
  expect(screen.getByRole("status").textContent).toBe("找到 0 段匹配文稿");
});

it("opens the page containing a citation and clears an unrelated search", () => {
  const view = render(
    <TranscriptReader
      transcript={transcript}
      sourceUrl="https://youtu.be/example"
    />,
  );
  fireEvent.change(screen.getByRole("searchbox", { name: "搜索文稿" }), {
    target: { value: "第 1 段" },
  });
  view.rerender(
    <TranscriptReader
      transcript={transcript}
      sourceUrl="https://youtu.be/example"
      focus={{
        id: 1,
        start: 2200,
        end: 2209,
        quote: "文稿第 220 段内容",
        approximate: true,
      }}
    />,
  );
  expect(
    (screen.getByRole("searchbox", { name: "搜索文稿" }) as HTMLInputElement)
      .value,
  ).toBe("");
  expect(document.getElementById("transcript-segment-220")?.className).toBe(
    "citation-highlight",
  );
  expect(document.querySelectorAll(".transcript-segments > div")).toHaveLength(
    50,
  );
});

it("copies and exports the complete transcript even when the view is filtered", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  render(
    <TranscriptReader
      transcript={transcript}
      sourceUrl="https://youtu.be/example"
    />,
  );
  fireEvent.change(screen.getByRole("searchbox", { name: "搜索文稿" }), {
    target: { value: "第 220 段" },
  });
  fireEvent.click(screen.getByRole("button", { name: "复制全文" }));
  await screen.findByText("全文已复制");
  expect(writeText).toHaveBeenCalledWith(
    expect.stringContaining("文稿第 249 段内容"),
  );
  fireEvent.click(screen.getByRole("button", { name: "下载 TXT" }));
  expect(downloadText).toHaveBeenLastCalledWith(
    expect.stringContaining("文稿第 249 段内容"),
    "videohub-transcript.txt",
    "text/plain;charset=utf-8",
  );
  fireEvent.click(screen.getByRole("button", { name: "下载 SRT" }));
  expect(downloadText).toHaveBeenLastCalledWith(
    expect.stringContaining(
      "250\n00:41:30,000 --> 00:41:39,000\n文稿第 249 段内容",
    ),
    "videohub-transcript.srt",
    "application/x-subrip;charset=utf-8",
  );
  writeText.mockRejectedValueOnce(new Error("denied"));
  fireEvent.click(screen.getByRole("button", { name: "复制全文" }));
  expect(
    await screen.findByText("复制失败，可下载 TXT 文件后复制。"),
  ).toBeTruthy();
});

it("exports SRT millisecond timestamps across minute and hour boundaries", () => {
  expect(
    transcriptToSrt([{ start: 59.9996, end: 3600.125, text: "跨小时字幕" }]),
  ).toBe("1\n00:01:00,000 --> 01:00:00,125\n跨小时字幕\n");
});

it("provides page recovery controls after a rendering failure", () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  function Broken(): never {
    throw new Error("simulated page failure");
  }
  render(
    <PageErrorBoundary>
      <Broken />
    </PageErrorBoundary>,
  );
  expect(screen.getByRole("alert").textContent).toContain(
    "已提交的任务会继续执行",
  );
  expect(screen.getByRole("button", { name: "刷新页面" })).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "返回首页" }).getAttribute("href"),
  ).toBe("/");
});

it("opens mobile navigation, closes it on Escape and restores trigger focus", () => {
  render(
    <MemoryRouter>
      <MobileNavigation />
    </MemoryRouter>,
  );
  const trigger = screen.getByRole("button", { name: "更多导航" });
  fireEvent.click(trigger);
  expect(screen.getByRole("navigation", { name: "网站导航" })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole("link", { name: "我的知识库" }), {
    key: "Escape",
  });
  expect(screen.queryByRole("navigation", { name: "网站导航" })).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

it("keeps recovery actions available while technical error details are collapsed", () => {
  const retry = vi.fn();
  const view = render(
    <ErrorNotice
      error={
        new ApiError("ASR_NOT_CONFIGURED", "ASR API key missing", "req-help")
      }
    >
      <button onClick={retry}>重新尝试</button>
    </ErrorNotice>,
  );
  expect(screen.getByText("这项功能暂时不可用")).toBeTruthy();
  const details = screen.getByText("查看详情").closest("details")!;
  expect(details.open).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "重新尝试" }));
  expect(retry).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByText("查看详情"));
  expect(details.open).toBe(true);
  expect(screen.getByText("ASR API key missing")).toBeTruthy();
  expect(screen.getByText("请求编号：req-help")).toBeTruthy();
  view.rerender(
    <ErrorNotice
      error={new ApiError("REQUEST_TIMEOUT", "upstream timeout", "req-timeout")}
    />,
  );
  expect(screen.getByText("查看详情").closest("details")!.open).toBe(false);
  expect(screen.getByText(/先查看任务历史确认处理状态/)).toBeTruthy();
});

it("preserves actionable validation messages and hides unknown technical failures in details", () => {
  const view = render(
    <ErrorNotice
      error={new ApiError("VIDEO_TOO_LONG", "视频超过当前权益的时长上限")}
    />,
  );
  expect(screen.getByText("视频超过当前权益的时长上限")).toBeTruthy();
  view.rerender(
    <ErrorNotice
      error={new ApiError("FUTURE_SERVICE_ERROR", "internal service exception")}
    />,
  );
  expect(screen.getByText("本次操作未能完成")).toBeTruthy();
  expect(
    screen.getByText("internal service exception").closest("details")!.open,
  ).toBe(false);
});
