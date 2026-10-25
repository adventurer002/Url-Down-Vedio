import { afterEach, expect, it, vi } from "vitest";
import { AxiosError } from "axios";
import { api } from "../api";
import { client, get, normalizeError, fileDownloadUrl } from "../api/client";
import { treeToMarkdown, timecode } from "../utils";
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
it("unwraps the backend data envelope", async () => {
  const adapter = client.defaults.adapter;
  client.defaults.adapter = async (config) => ({
    data: { data: { title: "video" } },
    status: 200,
    statusText: "OK",
    headers: {},
    config,
  });
  try {
    expect(await get("/test")).toEqual({ title: "video" });
  } finally {
    client.defaults.adapter = adapter;
  }
});
it("normalizes backend errors, including request ID", () => {
  const error = normalizeError(
    new AxiosError("bad", "ERR_BAD_REQUEST", undefined, undefined, {
      data: {
        error: {
          code: "SSRF_BLOCKED",
          message: "不能访问私网",
          request_id: "req_1",
        },
      },
      status: 400,
    } as never),
  );
  expect(error.code).toBe("SSRF_BLOCKED");
  expect(error.message).toBe("不能访问私网");
  expect(error.request_id).toBe("req_1");
});
it("uses safe opaque file IDs rather than server-supplied paths", () => {
  vi.stubGlobal("window", { location: { origin: "http://localhost:5173" } });
  expect(fileDownloadUrl("abc")).toBe(
    "http://localhost:8000/api/v1/files/abc/download",
  );
  expect(fileDownloadUrl("../secret")).toContain("..%2Fsecret");
});
it("sends exact endpoint contracts without exposing model or credentials", async () => {
  const post = vi
    .spyOn(client, "post")
    .mockResolvedValue({ data: { task_id: "one", status: "queued" } });
  await api.create("audio_extraction", "https://example.com");
  expect(post).toHaveBeenLastCalledWith("/audio/extractions", {
    url: "https://example.com",
    format: "mp3",
  });
  await api.create("download", "https://example.com", "video_720_mp4");
  expect(post).toHaveBeenLastCalledWith("/downloads", {
    url: "https://example.com",
    preset_id: "video_720_mp4",
  });
  await api.create("ai_summary", "https://example.com");
  expect(post).toHaveBeenLastCalledWith("/ai/summaries", {
    url: "https://example.com",
    language: "zh",
  });
  await api.create("transcription", "https://example.com");
  expect(post).toHaveBeenLastCalledWith("/transcriptions", {
    url: "https://example.com",
    language: null,
  });
});
it("formats chapter timestamps and produces escaped Markdown hierarchy", () => {
  expect(timecode(93.9)).toBe("01:33");
  expect(timecode(3601)).toBe("1:00:01");
  expect(timecode(null)).toBe("--:--");
  expect(
    treeToMarkdown({
      title: "<root>",
      children: [{ title: "chapter", children: [] }],
    }),
  ).toBe("# \\<root\\>\n## chapter\n");
});

it("quotes an operation then confirms only its immutable quote ID", async () => {
  const post = vi.spyOn(client, "post").mockResolvedValue({ data: {} });
  const controller = new AbortController();
  await api.quote(
    "ai_mindmap",
    "https://example.com/video",
    undefined,
    controller.signal,
  );
  expect(post).toHaveBeenLastCalledWith(
    "/billing/quotes",
    { action: "ai_mindmap", url: "https://example.com/video", language: "zh" },
    { signal: controller.signal },
  );
  await api.confirmQuote("quote-1");
  expect(post).toHaveBeenLastCalledWith("/billing/tasks", {
    quote_id: "quote-1",
  });
});
