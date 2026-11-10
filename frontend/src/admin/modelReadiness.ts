import { modelUses, type ModelCatalog, type ModelUse } from "./modelApi";

export function modelIssue(
  data: ModelCatalog,
  id: string,
  use: ModelUse,
): string | undefined {
  const resource = data.resources.find(
    (r) => r.kind === "model" && r.revisions.some((v) => v.id === id),
  );
  const model = resource?.revisions.find((v) => v.id === id);
  if (!model || resource?.archived) return "模型不存在或已归档";
  const connection = data.resources.find(
    (r) =>
      r.kind === "connection" &&
      r.revisions.some((v) => v.id === model.connection_revision),
  );
  const version = connection?.revisions.find(
    (v) => v.id === model.connection_revision,
  );
  if (!version || connection?.archived) return "服务连接不存在或已归档";
  if (!version.has_key || version.revoked) return "密钥不可用或已撤销";
  const family = ["transcription", "clip_timing"].includes(use) ? "asr" : "llm";
  if (model.family !== family) return "模型类型不匹配";
  if (use === "clip_timing" && version.adapter !== "openai_asr")
    return "需要精确分段时间戳";
  const proof = data.probes.find(
    (p) => p.revision_id === id && p.phase === "compatibility",
  );
  if (
    proof?.status !== "completed" ||
    !proof.result.capabilities?.includes(use)
  )
    return "需通过兼容测试";
}

export function bindingIssues(
  data: ModelCatalog,
  bindings: Record<ModelUse, string | null>,
): string[] {
  const enabled = data.enabled_uses || data.uses;
  return data.uses.flatMap((use) => {
    const required = enabled.includes(use) && use !== "transcription";
    const id = bindings[use];
    const issue = id
      ? modelIssue(data, id, use)
      : required
        ? "请选择模型"
        : undefined;
    return issue ? [`${modelUses[use]}：${issue}`] : [];
  });
}
