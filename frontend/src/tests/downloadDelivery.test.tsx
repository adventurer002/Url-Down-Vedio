// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { client } from "../api/client";
import TaskCard from "../components/TaskCard";
import { WorkspaceProvider } from "../hooks/useWorkspace";
import { authStore } from "../stores/auth";
import { billingStore } from "../stores/billing";
import { createWorkspace, type LocalTask } from "../stores/workspace";

const task = {
  id: "delivery-1",
  videoKey: "fixture",
  kind: "download",
  label: "720P MP4",
  title: "测试视频",
  event: { status: "completed", stage: "completed", progress: 100, seq: 3 },
  snapshot: {
    result_payload: { delivery_id: "delivery-1", delivery_mode: "relay" },
  },
} as LocalTask;

beforeEach(() => {
  authStore.accept({
    status: "authenticated",
    access_token: "test-session-token",
    user: {
      id: "delivery-user",
      nickname: "测试",
      role: "user",
      status: "active",
      is_subscribed: true,
    },
  });
  vi.spyOn(billingStore, "loadUsage").mockResolvedValue(undefined);
  vi.spyOn(api, "deliveryStatus").mockResolvedValue({
    active: false,
    started: true,
    last_error: null,
    fallback_task_id: null,
  });
});
afterEach(() => {
  cleanup();
  authStore.clear();
  vi.restoreAllMocks();
});

it("uses a scoped delivery ticket and describes browser handoff without claiming a local save", async () => {
  const store = createWorkspace();
  vi.spyOn(client, "post").mockResolvedValue({
    data: { access_token: "delivery-ticket" },
  });
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.href).toContain(
        "/deliveries/delivery-1/download?access_token=delivery-ticket",
      );
      expect(this.href).not.toContain("test-session-token");
    });
  render(
    <MemoryRouter>
      <WorkspaceProvider store={store}>
        <TaskCard task={task} />
      </WorkspaceProvider>
    </MemoryRouter>,
  );
  expect(screen.getByText("可开始下载")).toBeTruthy();
  expect(screen.getByText("边接收边下载")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "保存到本地" }));
  await screen.findByText(/已交给浏览器下载/);
  expect(click).toHaveBeenCalledTimes(1);
  expect(client.post).toHaveBeenCalledWith(
    "/deliveries/delivery-1/download/access",
    undefined,
    expect.any(Object),
  );
});

it("reuses the original delivery for a server-render fallback", async () => {
  const store = createWorkspace();
  const fallback = vi
    .spyOn(store, "prepareDownloadFallback")
    .mockResolvedValue(task);
  render(
    <MemoryRouter>
      <WorkspaceProvider store={store}>
        <TaskCard task={task} />
      </WorkspaceProvider>
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "改为生成成品" }));
  await waitFor(() => expect(fallback).toHaveBeenCalledWith(task));
  expect(
    await screen.findByRole("button", { name: "成品任务已创建" }),
  ).toHaveProperty("disabled", true);
});
