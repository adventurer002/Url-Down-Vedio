import { afterEach, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

it.each([
  ["/api/v1", "https://vedio.vip/api/v1"],
  ["/api/v1/", "https://vedio.vip/api/v1"],
  ["https://api.example.com/api/v1", "https://api.example.com/api/v1"],
  ["", "http://localhost:8000/api/v1"],
])("resolves downloads and SSE with API base %s", async (configured, expected) => {
  vi.resetModules();
  vi.stubEnv("VITE_API_BASE_URL", configured);
  vi.stubGlobal("window", { location: { origin: "https://vedio.vip" } });
  const { apiUrl, fileDownloadUrl } = await import("../api/client");
  expect(fileDownloadUrl("file-1")).toBe(`${expected}/files/file-1/download`);
  expect(fileDownloadUrl("../secret")).toBe(`${expected}/files/..%2Fsecret/download`);
  expect(apiUrl("/api/v1/tasks/task-1/events")).toBe(`${expected}/tasks/task-1/events`);
});
