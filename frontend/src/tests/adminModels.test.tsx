// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ModelsPage from "../admin/ModelsPage";
import { modelApi, modelUses, type ModelCatalog } from "../admin/modelApi";
import { authStore } from "../stores/auth";
import { ApiError } from "../api/client";
const connection = "11111111-1111-4111-8111-111111111111";
const model = "22222222-2222-4222-8222-222222222222";
const initial: ModelCatalog = {
  enabled: true,
  encryption_ready: true,
  version: 0,
  active_release: null,
  uses: Object.keys(modelUses) as (keyof typeof modelUses)[],
  enabled_uses: ["summary", "mindmap", "question", "transcription"],
  adapters: {
    openai_chat: { label: "OpenAI 兼容文本接口", family: "llm" },
    openai_asr: { label: "OpenAI 兼容语音转写", family: "asr" },
    qwen_asr: { label: "千问语音转写", family: "asr" },
  },
  resources: [],
  probes: [],
  releases: [],
};
function configured() {
  const data = structuredClone(initial);
  data.resources = [
    {
      id: "connection",
      kind: "connection",
      name: "主力服务",
      version: 1,
      archived: false,
      revisions: [
        {
          id: connection,
          version: 1,
          created_at: "2026-11-01T00:00:00Z",
          adapter: "openai_chat",
          base_url: "https://api.example.com/v1",
          credential_id: "secret-id",
          has_key: true,
        },
      ],
    },
    {
      id: "model",
      kind: "model",
      name: "总结模型",
      version: 1,
      archived: false,
      revisions: [
        {
          id: model,
          version: 1,
          created_at: "2026-11-01T00:00:00Z",
          connection_revision: connection,
          model_id: "upstream-model",
          family: "llm",
          timeout: 90,
          max_tokens: 4096,
        },
      ],
    },
  ];
  return data;
}
function mount() {
  return render(
    <MemoryRouter>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ModelsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}
beforeEach(() => {
  sessionStorage.clear();
  authStore.clear();
  authStore.accept({
    status: "authenticated",
    access_token: "model-admin-token",
    user: {
      id: "model-admin",
      nickname: "管理员",
      role: "admin",
      status: "active",
      is_subscribed: true,
    },
  });
  vi.spyOn(modelApi, "catalog").mockResolvedValue(structuredClone(initial));
  vi.spyOn(modelApi, "mutate").mockResolvedValue({
    audit_id: "audit",
    duplicate: false,
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  authStore.clear();
  sessionStorage.clear();
});
it("shows configuration setup without inventing models or published status", async () => {
  vi.mocked(modelApi.catalog).mockResolvedValue({
    ...initial,
    encryption_ready: false,
    enabled: false,
  });
  mount();
  expect(
    await screen.findByText(/服务端尚未配置模型密钥加密主密钥/),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: "添加服务连接" })).toHaveProperty(
    "disabled",
    true,
  );
  fireEvent.click(screen.getByRole("button", { name: "功能分配" }));
  expect(screen.getByText(/尚未发布，项目沿用服务器环境配置/)).toBeTruthy();
});
it("keeps a secret write only in memory and replays the exact original request after timeout", async () => {
  vi.mocked(modelApi.mutate)
    .mockRejectedValueOnce(new ApiError("REQUEST_TIMEOUT", "提交超时"))
    .mockResolvedValue({ audit_id: "audit", duplicate: true });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "添加服务连接" }));
  const dialog = screen.getByRole("dialog", { name: "添加服务连接" });
  fireEvent.change(within(dialog).getByLabelText("配置名称"), {
    target: { value: "主力服务" },
  });
  fireEvent.change(within(dialog).getByLabelText("API Base URL"), {
    target: { value: "https://api.example.com/v1" },
  });
  fireEvent.change(within(dialog).getByLabelText("API Key"), {
    target: { value: "private-vendor-key" },
  });
  fireEvent.change(within(dialog).getByLabelText("操作原因"), {
    target: { value: "配置主力模型服务" },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "保存草稿" }));
  expect(await within(dialog).findByText("提交超时")).toBeTruthy();
  const first = vi.mocked(modelApi.mutate).mock.calls[0];
  expect(first[1]).toMatchObject({
    api_key: "private-vendor-key",
    adapter: "openai_chat",
  });
  expect(
    JSON.stringify(
      Object.fromEntries(
        Object.keys(sessionStorage).map((k) => [k, sessionStorage.getItem(k)]),
      ),
    ),
  ).not.toContain("private-vendor-key");
  fireEvent.click(
    within(dialog).getByRole("button", { name: "使用原请求重试" }),
  );
  await waitFor(() => expect(modelApi.mutate).toHaveBeenCalledTimes(2));
  expect(vi.mocked(modelApi.mutate).mock.calls[1]).toEqual(first);
});
it("requires business compatibility rather than connection success before function selection", async () => {
  const data = configured();
  data.probes = [
    {
      id: "probe",
      revision_id: model,
      phase: "connection",
      task_id: "task",
      status: "completed",
      created_at: "2026-11-01T00:00:00Z",
      result: { capabilities: [] },
    },
  ];
  vi.mocked(modelApi.catalog).mockResolvedValue(data);
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "功能分配" }));
  const summary = await screen.findByLabelText("章节总结");
  expect(
    within(summary).getByRole("option", { name: /需通过兼容测试/ }),
  ).toHaveProperty("disabled", true);
});
it("publishes all bindings together and retains the expected active generation", async () => {
  const data = configured();
  data.probes = [
    {
      id: "probe",
      revision_id: model,
      phase: "compatibility",
      task_id: "task",
      status: "completed",
      created_at: "2026-11-01T00:00:00Z",
      result: {
        capabilities: ["summary", "mindmap", "question", "clip_analysis"],
      },
    },
  ];
  vi.mocked(modelApi.catalog).mockResolvedValue(data);
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "功能分配" }));
  for (const name of ["章节总结", "思维导图", "内容问答"])
    fireEvent.change(await screen.findByLabelText(name), {
      target: { value: model },
    });
  fireEvent.change(screen.getByLabelText("发布原因"), {
    target: { value: "统一切换文本模型" },
  });
  expect(screen.getByRole("button", { name: "应用配置" })).toHaveProperty(
    "disabled",
    true,
  );
  fireEvent.click(
    screen.getByRole("checkbox", { name: "已核对功能分配和模型测试结果" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "应用配置" }));
  await waitFor(() =>
    expect(modelApi.mutate).toHaveBeenCalledWith(
      "publish",
      expect.objectContaining({
        expected_version: 0,
        bindings: {
          summary: model,
          mindmap: model,
          question: model,
          clip_analysis: null,
          transcription: null,
          clip_timing: null,
        },
      }),
    ),
  );
});
it("does not apply a response to a different administrator session", async () => {
  let finish!: (value: Record<string, unknown>) => void;
  vi.mocked(modelApi.catalog).mockResolvedValue(configured());
  vi.mocked(modelApi.mutate).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "连接测试" }));
  await waitFor(() => expect(modelApi.mutate).toHaveBeenCalled());
  act(() => authStore.clear());
  await act(async () => finish({ audit_id: "old-audit" }));
  expect(
    screen.queryByText("测试任务已创建，可在模型列表查看结果。"),
  ).toBeNull();
});

function compatible() {
  const data = configured();
  data.probes = [
    {
      id: "compatible",
      revision_id: model,
      phase: "compatibility",
      task_id: "tested",
      status: "completed",
      created_at: "2026-11-01T00:00:00Z",
      result: {
        capabilities: ["summary", "mindmap", "question", "clip_analysis"],
      },
    },
  ];
  return data;
}

it("disables the matching running test while allowing another test phase", async () => {
  const data = configured();
  data.probes = [
    {
      id: "running",
      revision_id: model,
      phase: "connection",
      task_id: "queued",
      status: "queued",
      created_at: "2026-11-01T00:00:00Z",
      result: {},
    },
  ];
  vi.mocked(modelApi.catalog).mockResolvedValue(data);
  mount();
  expect(
    await screen.findByRole("button", { name: "连接测试" }),
  ).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "兼容测试" })).toHaveProperty(
    "disabled",
    false,
  );
});

it("shows preflight issues and rejects revoked model options", async () => {
  const data = compatible();
  data.resources[0].revisions[0].has_key = false;
  data.resources[0].revisions[0].revoked = true;
  vi.mocked(modelApi.catalog).mockResolvedValue(data);
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "功能分配" }));
  const option = within(screen.getByLabelText("章节总结")).getByRole("option", {
    name: /密钥不可用或已撤销/,
  });
  expect(option).toHaveProperty("disabled", true);
  fireEvent.change(screen.getByLabelText("发布原因"), {
    target: { value: "准备发布" },
  });
  fireEvent.click(
    screen.getByRole("checkbox", { name: "已核对功能分配和模型测试结果" }),
  );
  expect(screen.getByRole("button", { name: "应用配置" })).toHaveProperty(
    "disabled",
    true,
  );
  expect(
    within(screen.getByRole("region", { name: "发布前检查" })).getByText(
      "章节总结：请选择模型",
    ),
  ).toBeTruthy();
  expect(modelApi.mutate).not.toHaveBeenCalled();
});

it("previews changes and confirms before discarding function assignments", async () => {
  vi.mocked(modelApi.catalog).mockResolvedValue(compatible());
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "功能分配" }));
  fireEvent.change(screen.getByLabelText("章节总结"), {
    target: { value: model },
  });
  fireEvent.change(screen.getByLabelText("发布原因"), {
    target: { value: "准备切换" },
  });
  const preview = screen.getByRole("region", { name: "发布前检查" });
  expect(
    within(preview).getAllByText(/服务器环境配置 → 总结模型/)[0],
  ).toBeTruthy();
  const unload = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(unload);
  expect(unload.defaultPrevented).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "重新载入当前分配" }));
  const dialog = await screen.findByRole("dialog", {
    name: "放弃未保存的修改？",
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "继续编辑" }));
  expect(screen.getByLabelText("章节总结")).toHaveProperty("value", model);
  fireEvent.click(screen.getByRole("button", { name: "重新载入当前分配" }));
  fireEvent.click(
    within(
      screen.getByRole("dialog", { name: "放弃未保存的修改？" }),
    ).getByRole("button", { name: "放弃修改" }),
  );
  expect(screen.getByLabelText("章节总结")).toHaveProperty("value", "");
  expect(screen.getByLabelText("发布原因")).toHaveProperty("value", "");
});

it("preserves editor fields when continuing and discards only after confirmation", async () => {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "添加服务连接" }));
  const editor = screen.getByRole("dialog", { name: "添加服务连接" });
  fireEvent.change(within(editor).getByLabelText("配置名称"), {
    target: { value: "尚未保存" },
  });
  fireEvent.click(within(editor).getByRole("button", { name: "取消" }));
  fireEvent.click(
    within(
      screen.getByRole("dialog", { name: "放弃未保存的修改？" }),
    ).getByRole("button", { name: "继续编辑" }),
  );
  expect(within(editor).getByLabelText("配置名称")).toHaveProperty(
    "value",
    "尚未保存",
  );
  fireEvent.click(within(editor).getByRole("button", { name: "取消" }));
  fireEvent.click(
    within(
      screen.getByRole("dialog", { name: "放弃未保存的修改？" }),
    ).getByRole("button", { name: "放弃修改" }),
  );
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(modelApi.mutate).not.toHaveBeenCalled();
});

it("loads shared key impact before confirming a version-checked revocation", async () => {
  vi.mocked(modelApi.catalog).mockResolvedValue(configured());
  let finish!: (
    value: Awaited<ReturnType<typeof modelApi.credentialImpact>>,
  ) => void;
  vi.spyOn(modelApi, "credentialImpact").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  mount();
  await screen.findByRole("table", { name: "服务连接" });
  const trigger = within(
    screen.getByRole("table", { name: "服务连接" }),
  ).getByRole("button", { name: "更多操作" });
  fireEvent.keyDown(trigger, { key: "ArrowDown" });
  fireEvent.click(await screen.findByRole("menuitem", { name: "撤销密钥" }));
  const dialog = await screen.findByRole("dialog", { name: "撤销此密钥版本" });
  fireEvent.change(within(dialog).getByLabelText("操作原因"), {
    target: { value: "紧急撤销" },
  });
  fireEvent.click(
    within(dialog).getByRole("checkbox", { name: "我已核对影响" }),
  );
  expect(
    within(dialog).getByRole("button", { name: "确认操作" }),
  ).toHaveProperty("disabled", true);
  await act(async () =>
    finish({
      credential_id: "secret-id",
      revoked: false,
      version: 4,
      connections: [
        { id: connection, name: "主力服务", version: 1, archived: false },
      ],
      models: [
        {
          id: model,
          name: "当前模型",
          version: 1,
          archived: false,
          active_uses: ["summary", "question"],
        },
      ],
    }),
  );
  expect(
    await within(dialog).findByText("正在使用：章节总结、内容问答"),
  ).toBeTruthy();
  expect(
    within(dialog).getByRole("checkbox", { name: "我已核对影响" }),
  ).toHaveProperty("checked", false);
  fireEvent.click(
    within(dialog).getByRole("checkbox", { name: "我已核对影响" }),
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "确认操作" }));
  await waitFor(() =>
    expect(modelApi.mutate).toHaveBeenCalledWith(
      "credentials/secret-id/revoke",
      expect.objectContaining({ expected_version: 4, reason: "紧急撤销" }),
    ),
  );
});

it("shows operator and reason in history and prevents identical re-publication", async () => {
  const data = compatible();
  data.version = 2;
  data.active_release = "release";
  data.releases = [
    {
      id: "release",
      created_at: "2026-11-01T00:00:00Z",
      actor_label: "值班管理员",
      reason: "替换总结服务",
      version: 2,
      bindings: {
        summary: model,
        mindmap: model,
        question: model,
        clip_analysis: model,
        transcription: null,
        clip_timing: null,
      },
    },
  ];
  vi.mocked(modelApi.catalog).mockResolvedValue(data);
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "功能分配" }));
  expect(screen.getByText("操作人：值班管理员")).toBeTruthy();
  expect(screen.getByText("原因：替换总结服务")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("发布原因"), {
    target: { value: "重复应用" },
  });
  fireEvent.click(
    screen.getByRole("checkbox", { name: "已核对功能分配和模型测试结果" }),
  );
  expect(screen.getByRole("button", { name: "应用配置" })).toHaveProperty(
    "disabled",
    true,
  );
});

it("reloads the published generation before clearing the assignment draft", async () => {
  const data = compatible();
  const published = structuredClone(data);
  published.version = 1;
  published.active_release = "new-release";
  published.releases = [
    {
      id: "new-release",
      created_at: "2026-11-08T00:00:00Z",
      version: 1,
      actor_label: "管理员",
      reason: "应用模型",
      bindings: {
        summary: model,
        mindmap: model,
        question: model,
        clip_analysis: null,
        transcription: null,
        clip_timing: null,
      },
    },
  ];
  vi.mocked(modelApi.catalog)
    .mockResolvedValueOnce(data)
    .mockResolvedValue(published);
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "功能分配" }));
  fireEvent.change(screen.getByLabelText("章节总结"), {
    target: { value: model },
  });
  fireEvent.change(screen.getByLabelText("发布原因"), {
    target: { value: "应用模型" },
  });
  fireEvent.click(
    screen.getByRole("checkbox", { name: "已核对功能分配和模型测试结果" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "应用配置" }));
  await screen.findByText("当前已发布版本 1");
  await waitFor(() =>
    expect(screen.queryByText("当前配置已更新。请重新载入后核对。")).toBeNull(),
  );
  expect(screen.getByLabelText("发布原因")).toHaveProperty("value", "");
  expect(screen.getByLabelText("章节总结")).toHaveProperty("value", model);
});
