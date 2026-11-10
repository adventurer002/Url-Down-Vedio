import "./models.css";
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import * as Dialog from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { bindingIssues, modelIssue } from "./modelReadiness";
import { useDiscardGuard } from "./useDiscardGuard";
import {
  modelApi,
  modelUses,
  type ModelCatalog,
  type ModelResource,
  type ModelUse,
} from "./modelApi";
import { authStore, useAuth } from "../stores/auth";
import { normalizeError } from "../api/client";
import { uncertainFailure, validReason } from "./operations";
import { ErrorState, Loading, Panel, formatDateTime } from "./ui";
import Icon from "../components/Icon";

const pendingStatuses = ["pending", "queued", "running", "processing"];
const probeErrors: Record<string, string> = {
  LLM_TIMEOUT: "模型请求超时，请检查地址或超时设置",
  ASR_TIMEOUT: "转写请求超时，请检查服务或超时设置",
  MODEL_TEST_TIMEOUT: "测试超过总时限",
  MODEL_COMPATIBILITY_FAILED: "未通过业务兼容测试",
  LLM_INVALID_RESPONSE: "模型返回内容不符合项目格式",
  MODEL_SCHEMA_INVALID: "模型未返回有效 JSON 结果",
  LLM_API_ERROR: "模型服务请求失败，请检查密钥、模型 ID 和服务额度",
  ASR_API_ERROR: "语音服务请求失败，请检查配置与额度",
  MODEL_TRANSCRIPT_MISMATCH: "固定语音识别结果不匹配",
  MODEL_TIMESTAMPS_INVALID: "模型未返回有效分段时间戳",
  MODEL_CREDENTIAL_REVOKED: "密钥已撤销",
  MODEL_TEST_AUTH_REVOKED: "发起测试的管理员权限已失效",
  TASK_CANCELLED: "测试已取消",
  MODEL_TEST_NOT_CREATED: "测试任务未创建，请重新发起测试",
};
type Write = (
  path: string,
  body: Record<string, unknown>,
  done?: () => void,
) => Promise<void>;
function versions(data: ModelCatalog, kind: "connection" | "model") {
  return data.resources
    .filter((r) => r.kind === kind && !r.archived)
    .flatMap((r) =>
      r.revisions.map((v) => ({ ...v, label: `${r.name} · v${v.version}` })),
    );
}
function liveBindings(data: ModelCatalog) {
  return (
    data.releases.find((r) => r.id === data.active_release)?.bindings ||
    (Object.fromEntries(data.uses.map((use) => [use, null])) as Record<
      ModelUse,
      string | null
    >)
  );
}

function MoreActions({
  items,
}: {
  items: {
    label: string;
    run: () => void;
    disabled?: boolean;
    danger?: boolean;
  }[];
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger className="admin-button" aria-label="更多操作">
        更多
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          className="admin-model-menu"
          align="end"
          sideOffset={6}
        >
          {items.map((item) => (
            <DropdownMenu.Item
              key={item.label}
              className={`admin-model-menu-item${item.danger ? " admin-model-danger-text" : ""}`}
              disabled={item.disabled}
              onSelect={item.run}
            >
              {item.label}
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function Editor({
  data,
  kind,
  resource,
  close,
  write,
  busy,
  error,
  retry,
}: {
  data: ModelCatalog;
  kind: "connection" | "model";
  resource?: ModelResource;
  close: () => void;
  write: Write;
  busy: boolean;
  error: string;
  retry?: () => void;
}) {
  const old = resource?.revisions[0];
  const [name, setName] = useState(resource?.name || "");
  const [adapter, setAdapter] = useState(old?.adapter || "openai_chat");
  const [url, setUrl] = useState(old?.base_url || "");
  const [secret, setSecret] = useState("");
  const connections = versions(data, "connection");
  const [connection, setConnection] = useState(
    old?.connection_revision || connections[0]?.id || "",
  );
  const [model, setModel] = useState(old?.model_id || "");
  const [timeout, setTimeoutValue] = useState(String(old?.timeout || 90));
  const [tokens, setTokens] = useState(String(old?.max_tokens || 4096));
  const [upload, setUpload] = useState(String(old?.max_upload_mb || 25));
  const [inputPrice, setInputPrice] = useState(old?.input_price || "");
  const [outputPrice, setOutputPrice] = useState(old?.output_price || "");
  const [reason, setReason] = useState("");
  const fields = {
    name,
    adapter,
    url,
    secret,
    connection,
    model,
    timeout,
    tokens,
    upload,
    inputPrice,
    outputPrice,
    reason,
  };
  const original = useRef(JSON.stringify(fields));
  const dirty = JSON.stringify(fields) !== original.current;
  const { requestDiscard, discardDialog } = useDiscardGuard(dirty);
  const requestClose = () => {
    if (!busy) requestDiscard(close);
  };
  const family =
    data.adapters[connections.find((c) => c.id === connection)?.adapter || ""]
      ?.family;
  return (
    <>
      <Dialog.Root
        open
        onOpenChange={(open) => {
          if (!open) requestClose();
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="admin-overlay" />
          <Dialog.Content className="admin-dialog admin-model-drawer">
            <Dialog.Title>
              {resource ? "编辑" : "添加"}
              {kind === "connection" ? "服务连接" : "模型"}
            </Dialog.Title>
            <Dialog.Description>
              {kind === "connection"
                ? "维护接口地址与密钥，多个模型可以复用同一连接。"
                : "保存后生成新版本；测试通过并应用到功能后生效。"}
            </Dialog.Description>
            {error && (
              <div role="alert" className="admin-error">
                {error}
                {retry && (
                  <button
                    type="button"
                    className="admin-button"
                    onClick={retry}
                  >
                    使用原请求重试
                  </button>
                )}
              </div>
            )}
            <Dialog.Close
              className="admin-icon-button admin-dialog-close"
              aria-label="关闭配置"
            >
              <Icon name="close" />
            </Dialog.Close>
            <form
              className="admin-model-form"
              onSubmit={(e) => {
                e.preventDefault();
                if (!validReason(reason) || busy) return;
                const common = {
                  id: resource?.id || null,
                  expected_version: resource?.version || 0,
                  name,
                  reason,
                };
                const body =
                  kind === "connection"
                    ? {
                        ...common,
                        adapter,
                        base_url: url,
                        ...(secret ? { api_key: secret } : {}),
                      }
                    : {
                        ...common,
                        connection_revision: connection,
                        model_id: model,
                        timeout: Number(timeout),
                        max_tokens: Number(tokens),
                        max_upload_mb: Number(upload),
                        input_price: inputPrice ? Number(inputPrice) : null,
                        output_price: outputPrice ? Number(outputPrice) : null,
                      };
                void write(
                  kind === "connection" ? "connections" : "models",
                  body,
                  () => {
                    setSecret("");
                    close();
                  },
                );
              }}
            >
              <label>
                配置名称
                <input
                  required
                  maxLength={80}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={
                    kind === "connection"
                      ? "例如：主力模型服务"
                      : "例如：日常总结模型"
                  }
                />
              </label>
              {kind === "connection" ? (
                <>
                  <label>
                    后端适配类型
                    <select
                      value={adapter}
                      onChange={(e) => setAdapter(e.target.value)}
                    >
                      {Object.entries(data.adapters).map(([id, a]) => (
                        <option key={id} value={id}>
                          {a.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    API Base URL
                    <input
                      required
                      type="url"
                      maxLength={2048}
                      value={url}
                      onChange={(e) => setUrl(e.target.value)}
                      placeholder="https://api.example.com/v1"
                    />
                  </label>
                  <label>
                    API Key
                    <input
                      required={!resource}
                      type="password"
                      autoComplete="new-password"
                      value={secret}
                      onChange={(e) => setSecret(e.target.value)}
                      placeholder={
                        resource ? "留空保留原密钥" : "输入服务商密钥"
                      }
                    />
                  </label>
                  <p className="admin-footnote">
                    密钥加密保存，保存后不回显。变更连接会生成新版本；模型需选择新连接版本并重新测试后发布。
                  </p>
                </>
              ) : (
                <>
                  <label>
                    服务连接版本
                    <select
                      required
                      value={connection}
                      onChange={(e) => setConnection(e.target.value)}
                    >
                      <option value="">请选择服务连接</option>
                      {connections.map((c) => (
                        <option disabled={!c.has_key} value={c.id} key={c.id}>
                          {c.label}
                          {c.has_key ? "" : "（密钥不可用）"}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    模型 ID
                    <input
                      required
                      maxLength={128}
                      value={model}
                      onChange={(e) => setModel(e.target.value)}
                      placeholder="填写服务商提供的模型 ID"
                    />
                  </label>
                  <div className="admin-model-field-row">
                    <label>
                      请求超时（秒）
                      <input
                        required
                        type="number"
                        min="5"
                        max="600"
                        value={timeout}
                        onChange={(e) => setTimeoutValue(e.target.value)}
                      />
                    </label>
                    {family === "asr" ? (
                      <label>
                        单次音频上限（MB）
                        <input
                          required
                          type="number"
                          min="1"
                          max="100"
                          value={upload}
                          onChange={(e) => setUpload(e.target.value)}
                        />
                      </label>
                    ) : (
                      <label>
                        最大输出 Token
                        <input
                          required
                          type="number"
                          min="128"
                          max="16384"
                          value={tokens}
                          onChange={(e) => setTokens(e.target.value)}
                        />
                      </label>
                    )}
                  </div>
                  {family === "llm" && (
                    <details>
                      <summary>成本估算（可选）</summary>
                      <p className="admin-footnote">
                        美元 / 百万
                        Token；用于总结与导图成本估算，不改变用户额度。留空表示成本未知。
                      </p>
                      <div className="admin-model-field-row">
                        <label>
                          输入单价
                          <input
                            type="number"
                            min="0"
                            max="100000"
                            step="any"
                            value={inputPrice}
                            onChange={(e) => setInputPrice(e.target.value)}
                          />
                        </label>
                        <label>
                          输出单价
                          <input
                            type="number"
                            min="0"
                            max="100000"
                            step="any"
                            value={outputPrice}
                            onChange={(e) => setOutputPrice(e.target.value)}
                          />
                        </label>
                      </div>
                    </details>
                  )}
                </>
              )}
              <label>
                操作原因
                <textarea
                  required
                  maxLength={500}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              <div className="admin-detail-actions">
                <button
                  className="admin-button admin-button-primary"
                  disabled={busy || !validReason(reason)}
                >
                  保存草稿
                </button>
                <button
                  type="button"
                  className="admin-button"
                  disabled={busy}
                  onClick={requestClose}
                >
                  取消
                </button>
              </div>
            </form>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      {discardDialog}
    </>
  );
}
function Confirm({
  title,
  description,
  close,
  submit,
  busy,
  error,
  retry,
  credentialId,
}: {
  title: string;
  description: string;
  close: () => void;
  submit: (reason: string, expectedVersion?: number) => void;
  credentialId?: string;
  busy: boolean;
  error: string;
  retry?: () => void;
}) {
  const [reason, setReason] = useState("");
  const [checked, setChecked] = useState(false);
  const impact = useQuery({
    queryKey: ["admin", "models", "credential-impact", credentialId],
    queryFn: ({ signal }) => modelApi.credentialImpact(credentialId!, signal),
    enabled: !!credentialId,
    staleTime: 0,
  });
  useEffect(() => {
    if (credentialId) setChecked(false);
  }, [credentialId, impact.dataUpdatedAt]);
  const blocked =
    busy ||
    (!!credentialId &&
      (!impact.data ||
        impact.isFetching ||
        impact.isError ||
        impact.data.revoked));
  return (
    <Dialog.Root
      open
      onOpenChange={(v) => {
        if (!v) close();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="admin-overlay" />
        <div className="admin-modal-viewport">
          <Dialog.Content className="admin-dialog">
            <Dialog.Title>{title}</Dialog.Title>
            <Dialog.Description>{description}</Dialog.Description>
            {credentialId && (
              <section className="admin-model-impact" aria-label="密钥影响范围">
                <h3>影响范围</h3>
                {impact.isPending ? (
                  <Loading />
                ) : impact.isError ? (
                  <ErrorState
                    error={impact.error}
                    retry={() => void impact.refetch()}
                  />
                ) : (
                  impact.data && (
                    <>
                      {impact.data.revoked && (
                        <p role="alert">此密钥已撤销，请刷新配置。</p>
                      )}
                      <p>
                        {impact.data.connections.length} 个连接版本、
                        {impact.data.models.length} 个模型版本共用此密钥。
                      </p>
                      <ul>
                        {impact.data.connections.map((c) => (
                          <li key={c.id}>
                            {c.name} · v{c.version}
                            {c.archived ? "（已归档）" : ""}
                          </li>
                        ))}
                      </ul>
                      {impact.data.models.length ? (
                        <ul>
                          {impact.data.models.map((m) => (
                            <li key={m.id}>
                              <strong>
                                {m.name} · v{m.version}
                              </strong>
                              {m.archived ? "（已归档）" : ""}
                              <span>
                                {m.active_uses.length
                                  ? `正在使用：${m.active_uses.map((use) => modelUses[use]).join("、")}`
                                  : "当前未分配功能；历史任务仍可能引用"}
                              </span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p>暂无模型引用。</p>
                      )}
                      <p>
                        撤销会同时影响以上全部版本，包括当前功能的后续请求。已发出的请求可能继续完成。
                      </p>
                    </>
                  )
                )}
              </section>
            )}
            {error && (
              <div role="alert" className="admin-error">
                {error}
                {retry && (
                  <button
                    type="button"
                    className="admin-button"
                    onClick={retry}
                  >
                    使用原请求重试
                  </button>
                )}
              </div>
            )}
            <form
              className="admin-model-form"
              onSubmit={(e) => {
                e.preventDefault();
                if (!blocked && checked && validReason(reason))
                  submit(reason, impact.data?.version);
              }}
            >
              <label>
                操作原因
                <textarea
                  required
                  maxLength={500}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              <label className="admin-model-check">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={(e) => setChecked(e.target.checked)}
                />
                我已核对影响
              </label>
              <div className="admin-detail-actions">
                <button
                  className="admin-button admin-button-danger"
                  disabled={blocked || !checked || !validReason(reason)}
                >
                  确认操作
                </button>
                <button type="button" className="admin-button" onClick={close}>
                  取消
                </button>
              </div>
            </form>
          </Dialog.Content>
        </div>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Workspace({
  data,
  write,
  busy,
  error,
  retry,
  refresh,
}: {
  data: ModelCatalog;
  write: Write;
  busy: boolean;
  error: string;
  retry?: () => void;
  refresh: () => void;
}) {
  const [tab, setTab] = useState("models");
  const [editor, setEditor] = useState<{
    kind: "connection" | "model";
    resource?: ModelResource;
  }>();
  const [confirm, setConfirm] = useState<{
    title: string;
    description: string;
    path: string;
    body: Record<string, unknown>;
    credentialId?: string;
  }>();
  const [draft, setDraft] = useState<{
    version: number;
    bindings: Record<ModelUse, string | null>;
  }>();
  const [reason, setReason] = useState("");
  const [checked, setChecked] = useState(false);
  useEffect(() => {
    if (!draft)
      setDraft({ version: data.version, bindings: liveBindings(data) });
  }, [draft, data]);
  const bindings = liveBindings(data),
    models = versions(data, "model");
  const title = (id: string | null) => {
    if (!id) return "未分配";
    return models.find((m) => m.id === id)?.label || "历史配置版本";
  };
  const draftDirty =
    !!draft &&
    (data.uses.some((use) => draft.bindings[use] !== bindings[use]) ||
      !!reason.trim());
  const { requestDiscard, discardDialog } = useDiscardGuard(
    !editor && draftDirty,
  );
  const issues = draft ? bindingIssues(data, draft.bindings) : [];
  const changes = draft
    ? data.uses.filter((use) => draft.bindings[use] !== bindings[use])
    : [];
  const testActive = (id: string, phase: string) =>
    data.probes.some(
      (p) =>
        p.revision_id === id &&
        p.phase === phase &&
        pendingStatuses.includes(p.status),
    );
  function revokeKey(id: string, label: string) {
    setConfirm({
      title: label,
      description: "先核对所有共用此密钥的配置。撤销后无法通过回滚恢复。",
      path: `credentials/${id}/revoke`,
      body: {},
      credentialId: id,
    });
  }
  function proof(id: string, phase: string) {
    return data.probes.find((p) => p.revision_id === id && p.phase === phase);
  }
  function test(
    resource: ModelResource,
    phase: "connection" | "compatibility",
  ) {
    void write("tests", {
      revision_id: resource.revisions[0].id,
      phase,
      reason: `管理员发起${phase === "connection" ? "连接" : "业务兼容"}测试`,
    });
  }
  function archive(r: ModelResource) {
    setConfirm({
      title: `归档 ${r.name}`,
      description:
        "归档后不能用于新发布。任务历史与已创建任务的配置版本会保留。正在使用的配置须先替换功能分配。",
      path: `resources/${r.id}/archive`,
      body: { expected_version: r.version },
    });
  }
  return (
    <>
      <nav className="admin-model-tabs" aria-label="模型管理页面">
        <button
          aria-current={tab === "models" ? "page" : undefined}
          onClick={() => setTab("models")}
        >
          模型配置
        </button>
        <button
          aria-current={tab === "bindings" ? "page" : undefined}
          onClick={() => setTab("bindings")}
        >
          功能分配
        </button>
      </nav>
      {tab === "models" ? (
        <>
          <Panel
            title="服务连接"
            description="接口协议、地址和密钥独立维护。"
            action={
              <button
                className="admin-button admin-button-primary"
                disabled={busy || !data.encryption_ready}
                onClick={() => setEditor({ kind: "connection" })}
              >
                添加服务连接
              </button>
            }
          >
            <div className="admin-table-scroll">
              <table className="admin-table admin-model-table">
                <caption className="admin-sr-only">服务连接</caption>
                <thead>
                  <tr>
                    {[
                      "名称 / 版本",
                      "适配类型",
                      "API 地址",
                      "密钥状态",
                      "操作",
                    ].map((t) => (
                      <th key={t}>{t}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.resources
                    .filter((r) => r.kind === "connection")
                    .map((r) => {
                      const v = r.revisions[0];
                      return (
                        <tr key={r.id}>
                          <td data-label="名称 / 版本">
                            <strong>{r.name}</strong>
                            <small>
                              v{r.version}
                              {r.archived ? " · 已归档" : ""}
                            </small>
                          </td>
                          <td data-label="适配类型">
                            {data.adapters[v.adapter!]?.label}
                          </td>
                          <td data-label="API 地址">
                            <span className="admin-model-url">
                              {v.base_url}
                            </span>
                          </td>
                          <td data-label="密钥状态">
                            <span
                              className={`admin-badge ${v.has_key ? "admin-badge-completed" : "admin-badge-failed"}`}
                            >
                              {v.has_key ? "已配置" : "已撤销"}
                            </span>
                          </td>
                          <td data-label="操作">
                            <div className="admin-model-row-actions">
                              <button
                                className="admin-button"
                                disabled={busy || r.archived}
                                onClick={() =>
                                  setEditor({ kind: "connection", resource: r })
                                }
                              >
                                编辑
                              </button>
                              <MoreActions
                                items={[
                                  {
                                    label: "归档",
                                    run: () => archive(r),
                                    disabled: busy || r.archived,
                                  },
                                  ...(v.has_key
                                    ? [
                                        {
                                          label: "撤销密钥",
                                          run: () =>
                                            revokeKey(
                                              v.credential_id!,
                                              "撤销此密钥版本",
                                            ),
                                          disabled: busy,
                                          danger: true,
                                        },
                                      ]
                                    : []),
                                  ...r.revisions
                                    .slice(1)
                                    .filter(
                                      (old, index, all) =>
                                        old.has_key &&
                                        old.credential_id !== v.credential_id &&
                                        all.findIndex(
                                          (x) =>
                                            x.credential_id ===
                                            old.credential_id,
                                        ) === index,
                                    )
                                    .map((old) => ({
                                      label: `撤销历史 v${old.version} 密钥`,
                                      run: () =>
                                        revokeKey(
                                          old.credential_id!,
                                          `撤销历史 v${old.version} 密钥`,
                                        ),
                                      disabled: busy,
                                      danger: true,
                                    })),
                                ]}
                              />
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
              {!data.resources.some((r) => r.kind === "connection") && (
                <p className="admin-empty">先添加一个服务连接，再添加模型。</p>
              )}
            </div>
          </Panel>
          <Panel
            title="模型列表"
            description="测试只使用固定示例内容；可能产生少量服务商费用，不扣用户额度。"
            action={
              <button
                className="admin-button admin-button-primary"
                disabled={
                  busy || !versions(data, "connection").some((v) => v.has_key)
                }
                onClick={() => setEditor({ kind: "model" })}
              >
                添加模型
              </button>
            }
          >
            <div className="admin-table-scroll">
              <table className="admin-table admin-model-table">
                <caption className="admin-sr-only">模型列表</caption>
                <thead>
                  <tr>
                    {[
                      "名称 / 模型 ID",
                      "类型 / 使用情况",
                      "连接测试",
                      "业务兼容测试",
                      "近24小时运行",
                      "操作",
                    ].map((t) => (
                      <th key={t}>{t}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.resources
                    .filter((r) => r.kind === "model")
                    .map((r) => {
                      const v = r.revisions[0],
                        activeUses = Object.entries(bindings).filter(([, id]) =>
                          r.revisions.some((x) => x.id === id),
                        );
                      return (
                        <tr key={r.id}>
                          <td data-label="名称 / 模型 ID">
                            <strong>
                              {r.name}{" "}
                              <small>
                                v{v.version}
                                {r.archived ? " · 已归档" : ""}
                              </small>
                            </strong>
                            <span>{v.model_id}</span>
                          </td>
                          <td data-label="类型 / 使用情况">
                            {v.family === "llm" ? "文本生成" : "语音转写"}
                            <small>
                              {activeUses.length
                                ? activeUses
                                    .map(
                                      ([use, id]) =>
                                        `${modelUses[use as ModelUse]} v${r.revisions.find((x) => x.id === id)?.version}`,
                                    )
                                    .join("、")
                                : "尚未发布"}
                            </small>
                          </td>
                          {["connection", "compatibility"].map((phase) => {
                            const p = proof(v.id, phase);
                            return (
                              <td
                                key={phase}
                                data-label={
                                  phase === "connection"
                                    ? "连接测试"
                                    : "业务兼容测试"
                                }
                              >
                                <span
                                  className={`admin-badge ${p?.status === "completed" ? "admin-badge-completed" : p?.status === "failed" ? "admin-badge-failed" : ""}`}
                                >
                                  {!p
                                    ? "未测试"
                                    : p.status === "completed"
                                      ? phase === "compatibility" &&
                                        v.family === "llm" &&
                                        (p.result.capabilities?.length || 0) < 4
                                        ? "部分能力通过"
                                        : "最近测试通过"
                                      : p.status === "failed"
                                        ? "未通过"
                                        : p.status === "cancelled"
                                          ? "已取消"
                                          : "测试中"}
                                </span>
                                {p && (
                                  <small>
                                    {formatDateTime(
                                      p.result.tested_at || p.created_at,
                                    )}
                                    {p.result.duration_ms != null
                                      ? ` · ${(p.result.duration_ms / 1000).toFixed(1)} 秒`
                                      : ""}
                                  </small>
                                )}
                                {p?.result.failures &&
                                  Object.entries(p.result.failures).map(
                                    ([use, code]) => (
                                      <small key={use}>
                                        {modelUses[use as ModelUse]}：
                                        {probeErrors[code!] || code}
                                      </small>
                                    ),
                                  )}
                                {p?.error_code && (
                                  <small>
                                    {probeErrors[p.error_code] || p.error_code}
                                  </small>
                                )}
                                {p?.result.capabilities?.length ? (
                                  <small>
                                    {p.result.capabilities
                                      .map((c) => modelUses[c])
                                      .join("、")}
                                  </small>
                                ) : null}
                              </td>
                            );
                          })}
                          <td data-label="近24小时运行">
                            {(() => {
                              const stats = (data.health || [])
                                .filter(
                                  (h) =>
                                    h.scope === "runtime" &&
                                    r.revisions.some(
                                      (v) => v.id === h.revision_id,
                                    ),
                                )
                                .reduce(
                                  (a, h) => ({
                                    count: a.count + h.count,
                                    success: a.success + h.success_count,
                                    ms: a.ms + h.duration_ms,
                                  }),
                                  { count: 0, success: 0, ms: 0 },
                                );
                              return stats.count ? (
                                <>
                                  <span>
                                    {stats.count} 次 · 成功率{" "}
                                    {Math.round(
                                      (stats.success / stats.count) * 100,
                                    )}
                                    %
                                  </span>
                                  <small>
                                    平均{" "}
                                    {(stats.ms / stats.count / 1000).toFixed(1)}{" "}
                                    秒 · 不含测试
                                  </small>
                                </>
                              ) : (
                                <span>暂无运行数据</span>
                              );
                            })()}
                          </td>
                          <td data-label="操作">
                            <div className="admin-model-row-actions">
                              <button
                                className="admin-button"
                                disabled={busy || r.archived}
                                onClick={() =>
                                  setEditor({ kind: "model", resource: r })
                                }
                              >
                                编辑
                              </button>
                              <button
                                className="admin-button admin-button-secondary"
                                disabled={
                                  busy ||
                                  r.archived ||
                                  testActive(v.id, "connection")
                                }
                                onClick={() => test(r, "connection")}
                              >
                                连接测试
                              </button>
                              <button
                                className="admin-button admin-button-secondary"
                                disabled={
                                  busy ||
                                  r.archived ||
                                  testActive(v.id, "compatibility")
                                }
                                onClick={() => test(r, "compatibility")}
                              >
                                兼容测试
                              </button>
                              <MoreActions
                                items={[
                                  {
                                    label: "归档",
                                    run: () => archive(r),
                                    disabled: busy || r.archived,
                                  },
                                ]}
                              />
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
              {!data.resources.some((r) => r.kind === "model") && (
                <p className="admin-empty">
                  添加模型 ID，完成测试后到「功能分配」应用配置。
                </p>
              )}
            </div>
            {data.probes.some((p) => pendingStatuses.includes(p.status)) && (
              <div className="admin-model-probes">
                {data.probes
                  .filter((p) => pendingStatuses.includes(p.status))
                  .map((p) => (
                    <div key={p.id}>
                      <span>模型测试排队或执行中 · {title(p.revision_id)}</span>
                      {p.task_id && (
                        <button
                          className="admin-button"
                          onClick={() =>
                            void write(`__cancel/${p.task_id}`, {
                              reason: "管理员取消模型测试",
                            })
                          }
                          disabled={busy}
                        >
                          取消测试
                        </button>
                      )}
                    </div>
                  ))}
              </div>
            )}
          </Panel>
        </>
      ) : (
        <>
          <Panel
            title="功能分配"
            description="草稿统一发布，新建任务使用新版本，已创建任务继续使用原版本。"
          >
            {draft && (
              <form
                className="admin-model-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (
                    !busy &&
                    !issues.length &&
                    changes.length &&
                    checked &&
                    validReason(reason)
                  )
                    void write(
                      "publish",
                      {
                        expected_version: draft.version,
                        bindings: draft.bindings,
                        reason,
                      },
                      () => {
                        setDraft(undefined);
                        setReason("");
                        setChecked(false);
                      },
                    );
                }}
              >
                <p className="admin-footnote">
                  {data.active_release
                    ? `当前已发布版本 ${data.version}`
                    : "尚未发布，项目沿用服务器环境配置。"}
                </p>
                {draft.version !== data.version && (
                  <p role="alert" className="admin-error">
                    当前配置已更新。请重新载入后核对。
                  </p>
                )}
                <div className="admin-model-bindings">
                  {(data.enabled_uses || data.uses).map((use) => {
                    const family = ["transcription", "clip_timing"].includes(
                      use,
                    )
                      ? "asr"
                      : "llm";
                    return (
                      <label key={use}>
                        {modelUses[use]}
                        <select
                          aria-label={modelUses[use]}
                          value={draft.bindings[use] || ""}
                          onChange={(e) => {
                            const value = e.target.value || null;
                            const next = { ...draft.bindings, [use]: value };
                            if (use === "summary")
                              for (const other of [
                                "mindmap",
                                "question",
                                "clip_analysis",
                              ] as ModelUse[]) {
                                if (
                                  (data.enabled_uses || data.uses).includes(
                                    other,
                                  ) &&
                                  (!next[other] ||
                                    next[other] === draft.bindings.summary)
                                )
                                  next[other] =
                                    value &&
                                    proof(
                                      value,
                                      "compatibility",
                                    )?.result.capabilities?.includes(other)
                                      ? value
                                      : null;
                              }
                            setDraft({ ...draft, bindings: next });
                            setChecked(false);
                          }}
                        >
                          <option value="">
                            {family === "asr"
                              ? "停用此项模型能力"
                              : "请选择模型"}
                          </option>
                          {models
                            .filter((m) => m.family === family)
                            .map((m) => {
                              const issue = modelIssue(data, m.id, use);
                              const supported = !issue;
                              return (
                                <option
                                  key={m.id}
                                  value={m.id}
                                  disabled={!supported}
                                >
                                  {m.label} · {m.model_id}
                                  {supported ? "" : `（${issue}）`}
                                </option>
                              );
                            })}
                        </select>
                        <small>当前：{title(bindings[use])}</small>
                      </label>
                    );
                  })}
                </div>
                <button
                  type="button"
                  className="admin-text-button"
                  onClick={() =>
                    requestDiscard(() => {
                      setDraft({
                        version: data.version,
                        bindings: liveBindings(data),
                      });
                      setReason("");
                      setChecked(false);
                    })
                  }
                >
                  重新载入当前分配
                </button>
                <section
                  className="admin-model-preflight"
                  aria-label="发布前检查"
                >
                  <h3>发布前检查</h3>
                  {issues.length ? (
                    <ul>
                      {issues.map((issue) => (
                        <li key={issue}>{issue}</li>
                      ))}
                    </ul>
                  ) : (
                    <p>模型、连接、密钥及兼容测试检查通过。</p>
                  )}
                  <h3>本次变更</h3>
                  {changes.length ? (
                    <ul>
                      {changes.map((use) => (
                        <li key={use}>
                          <strong>{modelUses[use]}</strong>
                          <span>
                            {data.active_release
                              ? title(bindings[use])
                              : "服务器环境配置"}{" "}
                            →{" "}
                            {draft.bindings[use]
                              ? title(draft.bindings[use])
                              : "停用此项模型能力"}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p>暂无配置变更。</p>
                  )}
                  {!data.active_release && !draft.bindings.transcription && (
                    <p>首次发布后，语音转写将停用，不再沿用服务器环境配置。</p>
                  )}
                </section>
                <label>
                  发布原因
                  <textarea
                    required
                    maxLength={500}
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                </label>
                <label className="admin-model-check">
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={(e) => setChecked(e.target.checked)}
                  />
                  已核对功能分配和模型测试结果
                </label>
                <button
                  className="admin-button admin-button-primary"
                  disabled={
                    busy ||
                    !data.enabled ||
                    !checked ||
                    !validReason(reason) ||
                    draft.version !== data.version ||
                    issues.length > 0 ||
                    changes.length === 0
                  }
                >
                  应用配置
                </button>
              </form>
            )}
          </Panel>
          <Panel
            title="发布历史"
            description="回滚会重新验证模型、测试结果及密钥状态，已撤销密钥不会被恢复。"
          >
            <div className="admin-model-release-list">
              {data.releases.map((r, index) => (
                <div key={r.id}>
                  <span>
                    {formatDateTime(r.created_at)}
                    {r.version != null ? ` · 版本 ${r.version}` : ""}
                    {r.id === data.active_release ? " · 当前生效" : ""}
                    <small>
                      操作人：{r.actor_label || r.actor_id || "历史记录未提供"}
                    </small>
                    <small>原因：{r.reason || "历史记录未提供"}</small>
                    <small>
                      {Object.entries(r.bindings)
                        .filter(([, v]) => v)
                        .map(
                          ([use, id]) =>
                            `${modelUses[use as ModelUse]}：${title(id)}`,
                        )
                        .join("；")}
                    </small>
                  </span>
                  <button
                    className="admin-button"
                    disabled={busy || r.id === data.active_release}
                    onClick={() =>
                      setConfirm({
                        title: "回滚功能分配",
                        description:
                          "将此历史分配重新发布为一个新版本，已创建任务继续使用各自锁定的版本。",
                        path: "publish",
                        body: {
                          expected_version: data.version,
                          release_id: r.id,
                        },
                      })
                    }
                  >
                    回滚到此版本
                  </button>
                </div>
              ))}
              {!data.releases.length && <p>暂无发布记录。</p>}
            </div>
          </Panel>
        </>
      )}
      {editor && (
        <Editor
          key={editor.resource?.id || editor.kind}
          data={data}
          {...editor}
          error={error}
          retry={retry}
          close={() => setEditor(undefined)}
          write={write}
          busy={busy}
        />
      )}
      {confirm && (
        <Confirm
          {...confirm}
          error={error}
          retry={retry}
          close={() => setConfirm(undefined)}
          busy={busy}
          submit={(reason, expectedVersion) =>
            void write(
              confirm.path,
              {
                ...confirm.body,
                reason,
                ...(confirm.credentialId
                  ? { expected_version: expectedVersion }
                  : {}),
              },
              () => {
                setConfirm(undefined);
                setDraft(undefined);
              },
            )
          }
        />
      )}
      {discardDialog}
    </>
  );
}

export default function ModelsPage() {
  const auth = useAuth(),
    client = useQueryClient();
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const pending = useRef<
    | {
        path: string;
        body: Record<string, unknown>;
        owner: string;
        token: string;
        done?: () => void;
      }
    | undefined
  >(undefined);
  const query = useQuery({
    queryKey: ["admin", "models", auth.user?.id],
    queryFn: ({ signal }) => modelApi.catalog(signal),
    refetchInterval: (q) =>
      q.state.data?.probes.some((p) => pendingStatuses.includes(p.status))
        ? 5000
        : false,
    refetchIntervalInBackground: false,
  });
  useEffect(() => {
    pending.current = undefined;
    setError("");
    setSuccess("");
    setBusy(false);
    setUncertain(false);
  }, [auth.user?.id, auth.token]);
  async function submit(item: NonNullable<typeof pending.current>) {
    if (
      busy ||
      authStore.getState().user?.id !== item.owner ||
      authStore.getState().token !== item.token
    )
      return;
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      if (item.path.startsWith("__cancel/")) {
        const { adminApi } = await import("./api");
        await adminApi.cancelTask(
          item.path.slice(9),
          item.body as unknown as import("./api").OperationRequest,
        );
      } else await modelApi.mutate(item.path, item.body);
      if (
        authStore.getState().user?.id !== item.owner ||
        authStore.getState().token !== item.token
      )
        return;
      pending.current = undefined;
      setUncertain(false);
      setSuccess(
        item.path === "tests"
          ? "测试任务已创建，可在模型列表查看结果。"
          : "操作已确认。",
      );
      await client.invalidateQueries({
        queryKey: ["admin", "models", item.owner],
      });
      if (
        authStore.getState().user?.id === item.owner &&
        authStore.getState().token === item.token
      )
        item.done?.();
    } catch (cause) {
      if (
        authStore.getState().user?.id === item.owner &&
        authStore.getState().token === item.token
      ) {
        setError(normalizeError(cause).message);
        const unknown = uncertainFailure(cause);
        setUncertain(unknown);
        if (!unknown) pending.current = undefined;
      }
    } finally {
      if (
        authStore.getState().user?.id === item.owner &&
        authStore.getState().token === item.token
      )
        setBusy(false);
    }
  }
  const write: Write = async (path, body, done) => {
    if (pending.current || busy || !auth.user || !auth.token) return;
    const item = {
      path,
      body: { ...body, key: `web_${crypto.randomUUID()}` },
      owner: auth.user.id,
      token: auth.token,
      done,
    };
    pending.current = item;
    await submit(item);
  };
  return (
    <div className="admin-model-management">
      <div className="admin-page-heading">
        <div>
          <h1>模型管理</h1>
          <p>配置服务连接和模型，测试后统一应用到项目功能。</p>
        </div>
        <button className="admin-button" onClick={() => void query.refetch()}>
          刷新配置
        </button>
      </div>
      {error && (
        <div role="alert" className="admin-error">
          {error}
          {uncertain && (
            <p>
              结果待确认，原请求已保留。
              <button
                className="admin-button"
                disabled={busy}
                onClick={() => pending.current && void submit(pending.current)}
              >
                使用原请求重试
              </button>
            </p>
          )}
        </div>
      )}
      {success && (
        <p role="status" className="admin-model-success">
          {success}
        </p>
      )}
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        <>
          {!query.data.encryption_ready && (
            <p className="admin-info-strip">
              服务端尚未配置模型密钥加密主密钥。请完成服务端配置后添加连接。
            </p>
          )}
          {!query.data.enabled && (
            <p className="admin-info-strip">
              模型管理尚未启用，项目当前沿用环境配置；发布前需启用服务端模型管理。
            </p>
          )}
          <Workspace
            key={auth.user?.id}
            data={query.data}
            error={error}
            retry={
              uncertain
                ? () => pending.current && void submit(pending.current)
                : undefined
            }
            write={write}
            busy={busy || uncertain || !auth.token}
            refresh={() => void query.refetch()}
          />
        </>
      )}
    </div>
  );
}
