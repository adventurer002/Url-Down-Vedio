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
    openai_asr: {
      label: "OpenAI 兼容语音转写",
      family: "asr",
      supports_timestamps: true,
    },
    qwen_asr: {
      label: "千问语音转写",
      family: "asr",
      supports_timestamps: false,
    },
    qwen_filetrans: {
      label: "千问 Filetrans（支持时间戳）",
      family: "asr",
      supports_timestamps: true,
    },
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

it.each([
  ["qwen_filetrans", ["transcription", "clip_timing"], false],
  ["qwen_filetrans", ["transcription"], true],
  ["qwen_asr", ["transcription"], true],
] as const)(
  "checks timestamp compatibility for %s with %j",
  async (adapter, capabilities, disabled) => {
    const data = compatible();
    data.enabled_uses = data.uses;
    data.resources[0].revisions[0].adapter = adapter;
    data.resources[1].revisions[0].family = "asr";
    data.resources[1].revisions[0].model_id = "qwen-test-model";
    data.probes[0].result.capabilities = [...capabilities];
    vi.mocked(modelApi.catalog).mockResolvedValue(data);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "功能分配" }));
    const timing = screen.getByLabelText("剪辑时间定位");
    const option = within(timing).getByRole("option", {
      name: /qwen-test-model/,
    });
    expect(option).toHaveProperty("disabled", disabled);
    if (!disabled) {
      fireEvent.change(timing, { target: { value: model } });
      expect(timing).toHaveProperty("value", model);
    }
  },
);

it("offers the Filetrans connection and model ID guidance", async () => {
  vi.mocked(modelApi.catalog).mockResolvedValue(configured());
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "添加服务连接" }));
  const editor = screen.getByRole("dialog", { name: "添加服务连接" });
  fireEvent.change(within(editor).getByRole("combobox"), {
    target: { value: "qwen_filetrans" },
  });
  expect(
    within(editor).getByPlaceholderText(
      "https://dashscope.aliyuncs.com/api/v1",
    ),
  ).toBeTruthy();
  expect(
    within(editor).getByText(/云端文件由服务商在 48 小时后清理/),
  ).toBeTruthy();
});
it("saves model edits with an empty optional reason", async () => {
  vi.mocked(modelApi.catalog).mockResolvedValue(configured());
  mount();
  const table = await screen.findByRole("table", { name: "模型列表" });
  fireEvent.click(within(table).getByRole("button", { name: "编辑" }));
  const dialog = screen.getByRole("dialog", { name: "编辑模型" });
  expect(within(dialog).getByLabelText("操作原因")).toHaveProperty(
    "required",
    false,
  );
  fireEvent.change(within(dialog).getByLabelText("配置名称"), {
    target: { value: "更新的模型名称" },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "保存草稿" }));
  await waitFor(() =>
    expect(modelApi.mutate).toHaveBeenCalledWith(
      "models",
      expect.objectContaining({ name: "更新的模型名称", reason: "" }),
    ),
  );
});
it("explains missing timing models and confirmation beside the apply button", async () => {
  const data = compatible();
  data.enabled_uses = data.uses;
  vi.mocked(modelApi.catalog).mockResolvedValue(data);
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "功能分配" }));
  fireEvent.change(screen.getByLabelText("章节总结"), {
    target: { value: model },
  });
  const button = screen.getByRole("button", { name: "应用配置" });
  const explanation = document.getElementById(
    button.getAttribute("aria-describedby")!,
  );
  expect(explanation?.textContent).toContain("剪辑时间定位：请选择模型");
  expect(explanation?.textContent).toContain(
    "请勾选「已核对功能分配和模型测试结果」",
  );
  expect(screen.queryByText(/导出切片时用于精确定位/)).toBeNull();
  fireEvent.click(
    screen.getByRole("checkbox", { name: "已核对功能分配和模型测试结果" }),
  );
  expect(explanation?.textContent).not.toContain("请勾选");
  expect(button).toHaveProperty("disabled", true);
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
  expect(screen.getByLabelText("发布原因")).toHaveProperty("required", false);
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
        reason: "",
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

it.each(["connection", "model"] as const)(
  "deletes a %s directly and removes it from the list",
  async (kind) => {
    const data = configured();
    const deleted = structuredClone(data);
    deleted.resources.find((r) => r.kind === kind)!.archived = true;
    vi.mocked(modelApi.catalog)
      .mockResolvedValueOnce(data)
      .mockResolvedValue(deleted);
    mount();
    const tableName = kind === "connection" ? "服务连接" : "模型列表";
    const table = await screen.findByRole("table", { name: tableName });
    expect(
      within(table).queryByRole("button", { name: "更多操作" }),
    ).toBeNull();
    fireEvent.click(within(table).getByRole("button", { name: "删除" }));
    const dialog = screen.getByRole("dialog", {
      name: kind === "connection" ? "删除 主力服务" : "删除 总结模型",
    });
    expect(modelApi.mutate).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认删除" }));
    await waitFor(() =>
      expect(modelApi.mutate).toHaveBeenCalledWith(
        `resources/${kind}/archive`,
        expect.objectContaining({ expected_version: 1, reason: "" }),
      ),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(within(table).queryByRole("button", { name: "删除" })).toBeNull();
  },
);

it("keeps a configuration visible when deleting an assigned resource is rejected", async () => {
  vi.mocked(modelApi.catalog).mockResolvedValue(configured());
  vi.mocked(modelApi.mutate).mockRejectedValue(
    new ApiError(
      "MODEL_IN_USE",
      "请先替换功能分配，再删除此配置",
      undefined,
      409,
    ),
  );
  mount();
  const table = await screen.findByRole("table", { name: "模型列表" });
  fireEvent.click(within(table).getByRole("button", { name: "删除" }));
  const dialog = screen.getByRole("dialog", { name: "删除 总结模型" });
  fireEvent.click(within(dialog).getByRole("button", { name: "确认删除" }));
  expect(
    await within(dialog).findByText("请先替换功能分配，再删除此配置"),
  ).toBeTruthy();
  expect(within(table).getByText("总结模型")).toBeTruthy();
});

it("preserves assignment edits when deleting another model", async () => {
  const data = compatible();
  const unused = structuredClone(data.resources[1]);
  unused.id = "unused-model";
  unused.name = "备用模型";
  unused.revisions[0].id = "unused-revision";
  data.resources.push(unused);
  const deleted = structuredClone(data);
  deleted.resources[2].archived = true;
  vi.mocked(modelApi.catalog)
    .mockResolvedValueOnce(data)
    .mockResolvedValue(deleted);
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "功能分配" }));
  fireEvent.change(screen.getByLabelText("章节总结"), {
    target: { value: model },
  });
  fireEvent.click(screen.getByRole("button", { name: "模型配置" }));
  const row = screen.getByText("备用模型").closest("tr")!;
  fireEvent.click(within(row).getByRole("button", { name: "删除" }));
  fireEvent.click(
    within(screen.getByRole("dialog", { name: "删除 备用模型" })).getByRole(
      "button",
      { name: "确认删除" },
    ),
  );
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "功能分配" }));
  expect(screen.getByLabelText("章节总结")).toHaveProperty("value", model);
});

it("hides publication history and prevents identical re-publication", async () => {
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
  expect(screen.queryByText("操作人：值班管理员")).toBeNull();
  expect(screen.queryByText("发布历史")).toBeNull();
  expect(screen.queryByText("原因：替换总结服务")).toBeNull();
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
  expect(screen.getByRole("status").textContent).toContain(
    "暂无配置变更，请先调整功能分配",
  );
});

it("shows the latest model and preserves a currently assigned older model", async () => {
  const data = compatible();
  const latest = {
    ...data.resources[1].revisions[0],
    id: "new-model",
    version: 2,
  };
  const obsolete = { ...latest, id: "obsolete-model", version: 0 };
  data.resources[1].revisions = [
    latest,
    data.resources[1].revisions[0],
    obsolete,
  ];
  data.resources[1].version = 2;
  data.active_release = "release";
  data.releases = [
    {
      id: "release",
      created_at: "2026-11-01T00:00:00Z",
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
  vi.mocked(modelApi.catalog).mockResolvedValue(data);
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "功能分配" }));
  const summary = screen.getByLabelText("章节总结");
  expect(summary).toHaveProperty("value", model);
  const options = within(summary).getAllByRole("option") as HTMLOptionElement[];
  expect(options.map((option) => option.value)).toEqual([
    "",
    "new-model",
    model,
  ]);
  expect(options.some((option) => /v\d/.test(option.text))).toBe(false);
  expect(options[1]).toHaveProperty("disabled", true);
  expect(options[2]).toHaveProperty("disabled", false);
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
  await screen.findByText("当前配置已生效");
  await waitFor(() =>
    expect(screen.queryByText("当前配置已更新。请重新载入后核对。")).toBeNull(),
  );
  expect(screen.getByLabelText("发布原因")).toHaveProperty("value", "");
  expect(screen.getByLabelText("章节总结")).toHaveProperty("value", model);
});
