import { get } from "../api/client";
import { adminPost } from "./api";
export const modelUses = {
  summary: "章节总结",
  mindmap: "思维导图",
  question: "内容问答",
  clip_analysis: "剪辑内容分析",
  transcription: "语音转写",
  clip_timing: "剪辑时间定位",
} as const;
export type ModelUse = keyof typeof modelUses;
export interface ModelVersion {
  id: string;
  version: number;
  created_at: string;
  adapter?: string;
  base_url?: string;
  credential_id?: string;
  has_key?: boolean;
  revoked?: boolean;
  connection_revision?: string;
  family?: "llm" | "asr";
  model_id?: string;
  timeout?: number;
  max_tokens?: number;
  max_upload_mb?: number;
  input_price?: string | null;
  output_price?: string | null;
}
export interface ModelResource {
  id: string;
  kind: "connection" | "model";
  name: string;
  version: number;
  archived: boolean;
  revisions: ModelVersion[];
}
export interface ModelCatalog {
  enabled: boolean;
  encryption_ready: boolean;
  version: number;
  active_release: string | null;
  uses: ModelUse[];
  enabled_uses?: ModelUse[];
  adapters: Record<string, { label: string; family: "llm" | "asr" }>;
  resources: ModelResource[];
  health?: {
    revision_id: string;
    scope: "test" | "runtime";
    count: number;
    success_count: number;
    duration_ms: number;
  }[];
  probes: {
    id: string;
    revision_id: string;
    phase: "connection" | "compatibility";
    task_id: string | null;
    status: string;
    error_code?: string | null;
    created_at: string;
    result: {
      capabilities?: ModelUse[];
      failures?: Partial<Record<ModelUse, string>>;
      duration_ms?: number;
      tested_at?: string;
      checks?: string[];
    };
  }[];
  releases: {
    id: string;
    bindings: Record<ModelUse, string | null>;
    created_at: string;
    actor_id?: string | null;
    actor_label?: string;
    reason?: string;
    version?: number;
  }[];
}
export interface CredentialImpact {
  credential_id: string;
  revoked: boolean;
  version: number;
  connections: {
    id: string;
    name: string;
    version: number;
    archived: boolean;
  }[];
  models: {
    id: string;
    name: string;
    version: number;
    archived: boolean;
    active_uses: ModelUse[];
  }[];
}
export const modelApi = {
  catalog: (signal?: AbortSignal) => get<ModelCatalog>("/admin/models", signal),
  credentialImpact: (id: string, signal?: AbortSignal) =>
    get<CredentialImpact>(`/admin/models/credentials/${id}/impact`, signal),
  mutate: (path: string, body: Record<string, unknown>, signal?: AbortSignal) =>
    adminPost<Record<string, unknown>>(
      `/admin/models/${path}`,
      body as unknown as import("./api").OperationRequest,
      signal,
    ),
};
