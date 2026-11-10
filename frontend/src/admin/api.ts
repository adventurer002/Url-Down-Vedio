import { ApiError, client, get } from "../api/client";
import { authStore } from "../stores/auth";

export type TaskStatus =
  | "queued"
  | "running"
  | "processing"
  | "completed"
  | "failed"
  | "cancelled"
  | "expired";
export interface Overview {
  as_of: string;
  business_date: string;
  timezone: string;
  users: {
    total: number;
    active: number;
    disabled: number;
    subscribed: number;
  };
  tasks: {
    total: number;
    created_today: number;
    by_status: Record<TaskStatus, number>;
  };
  allowances: {
    valid_memberships: number;
    usage: {
      asr_seconds: { consumed: number; held: number };
      ai_generations: { consumed: number; held: number };
    };
  };
  worker: { pool: string; concurrency: number; liveness: string };
}
export interface StorageStatus {
  status: "ready" | "partial";
  used_bytes: number | null;
  observed_bytes: number;
  disk_free_bytes: number | null;
  scan_complete: boolean;
  quota_bytes: number;
  min_free_bytes: number;
  file_ttl_hours: number;
}
export interface AdminTask {
  task_id: string;
  user_id: string | null;
  task_type: string;
  status: TaskStatus;
  progress: number;
  cancel_requested: boolean;
  error_code: string | null;
  wait_reason: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  queue_seconds: number;
  execution_seconds: number | null;
  attempts?: number;
  history_hidden?: boolean;
  expires_at?: string | null;
  stage?: string;
  seq?: number;
  allowance?: {
    state: string;
    business_date: string;
    maximum: Record<AllowanceKind, number>;
    measured_asr_seconds: number;
    settlement: Record<string, unknown> | null;
  } | null;
  video: { title: string; platform: string; duration: number | null } | null;
}
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
export const adminApi = {
  audit: (filters: AuditFilters, signal?: AbortSignal) =>
    get<Page<AuditEvent>>(
      `/admin/audit-events?${queryString(filters)}`,
      signal,
    ),
  cleanup: (body: OperationRequest, signal?: AbortSignal) =>
    adminPost<CleanupReceipt>("/admin/storage/cleanup", body, signal, 110000),
  changeStatus: (id: string, body: StatusRequest, signal?: AbortSignal) =>
    adminPost<StatusResult>(
      `/admin/users/${encodeURIComponent(id)}/status`,
      body,
      signal,
    ),
  grantPreview: (id: string, product: GrantProduct, signal?: AbortSignal) =>
    get<GrantPreview>(
      `/admin/users/${encodeURIComponent(id)}/grants/preview?product=${product}`,
      signal,
    ),
  grant: (id: string, body: GrantRequest, signal?: AbortSignal) =>
    adminPost<GrantResult>(
      `/admin/users/${encodeURIComponent(id)}/grants`,
      body,
      signal,
    ),
  cancelTask: (id: string, body: OperationRequest, signal?: AbortSignal) =>
    adminPost<CancelResult>(
      `/admin/tasks/${encodeURIComponent(id)}/cancel`,
      body,
      signal,
    ),
  tasks: (filters: TaskFilters, signal?: AbortSignal) =>
    get<Page<AdminTask>>(`/admin/tasks?${queryString(filters)}`, signal),
  users: (filters: UserFilters, signal?: AbortSignal) =>
    get<Page<AdminUser>>(`/admin/users?${queryString(filters)}`, signal),
  user: (id: string, signal?: AbortSignal) =>
    get<AdminUser>(`/admin/users/${encodeURIComponent(id)}`, signal),
  allowances: (id: string, offset = 0, signal?: AbortSignal) =>
    get<AdminAllowances>(
      `/admin/users/${encodeURIComponent(id)}/allowances?limit=20&offset=${offset}`,
      signal,
    ),
  ledger: (id: string, offset = 0, signal?: AbortSignal) =>
    get<Page<LedgerEvent>>(
      `/admin/users/${encodeURIComponent(id)}/ledger?limit=20&offset=${offset}`,
      signal,
    ),
  overview: (signal?: AbortSignal) => get<Overview>("/admin/overview", signal),
  storage: (signal?: AbortSignal) =>
    get<StorageStatus>("/admin/storage", signal),
  recentTasks: (signal?: AbortSignal) =>
    get<Page<AdminTask>>("/admin/tasks?limit=5&offset=0", signal),
  task: (id: string, signal?: AbortSignal) =>
    get<AdminTask>(`/admin/tasks/${encodeURIComponent(id)}`, signal),
};

export interface TaskFilters {
  offset?: number;
  limit?: number;
  status?: string;
  task_type?: string;
  user_id?: string;
  since?: string;
  until?: string;
}
export interface UserFilters {
  offset?: number;
  limit?: number;
  q?: string;
  status?: string;
  subscribed?: string;
  member?: string;
}
export interface AdminUser {
  id: string;
  nickname: string;
  role: string;
  status: string;
  status_version: number;
  is_subscribed: boolean;
  member: boolean;
  member_until: string | null;
  created_at: string;
  last_login_at: string | null;
}
export type AllowanceKind = "asr_seconds" | "ai_generations";
export interface AllowanceLot {
  id: string;
  kind: AllowanceKind;
  source: string;
  granted: number;
  used: number;
  held: number;
  usable: number;
  starts_at: string;
  expires_at: string;
  expired: boolean;
  future: boolean;
}
export interface AdminAllowances {
  version: string;
  as_of: string;
  account_exists: boolean;
  account_active: boolean;
  subscribed: boolean;
  member: boolean;
  member_until: string | null;
  trial_claimed_at: string | null;
  balances: Record<
    AllowanceKind,
    { available: number; held: number; usable: number }
  >;
  limits: {
    video_seconds: number;
    file_bytes: number;
    daily_egress_bytes: number;
    download_concurrency: number;
  };
  daily: {
    date: string;
    downloads_used: number;
    downloads_held: number;
    egress_used_bytes: number;
    egress_held_bytes: number;
  };
  lots: Page<AllowanceLot>;
}
export interface LedgerEvent {
  id: string;
  event: string;
  detail: Record<string, unknown>;
  created_at: string;
}
function queryString(filters: object) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters))
    if (value !== "" && value != null) params.set(key, String(value));
  return params.toString();
}

export type GrantProduct = "monthly" | "asr_120" | "ai_100";
export interface OperationRequest {
  reason: string;
  key: string;
}
export interface StatusRequest extends OperationRequest {
  status: "active" | "disabled";
  expected_status: "active" | "disabled";
  expected_version: number;
}
export interface GrantRequest extends OperationRequest {
  product: GrantProduct;
}
export interface GrantPreview {
  product: GrantProduct;
  offer: {
    name: string;
    asr_seconds: number;
    ai_generations: number;
    price_fen: number;
  };
  starts_at: string;
  expires_at: string;
  member_until_before: string | null;
  mode: "operator_grant";
  payment_verified: false;
  note: string;
}
export interface WriteReceipt {
  audit_id: string;
  duplicate: boolean;
}
export interface StatusResult extends WriteReceipt {
  user_id: string;
  status: "active" | "disabled";
  status_version: number;
}
export interface GrantResult extends WriteReceipt {
  key: string;
  product: GrantProduct;
  starts_at: string;
  expires_at: string;
  mode: "operator_grant";
  payment_verified: false;
  dispatched: number | null;
  dispatch_pending: boolean;
}
export interface CancelResult extends WriteReceipt {
  task_id: string;
  status: TaskStatus;
  cancel_requested: boolean;
  notification_pending: boolean;
}
export async function adminPost<T>(
  path: string,
  body: OperationRequest,
  signal?: AbortSignal,
  timeout = 15000,
): Promise<T> {
  const auth = authStore.getState();
  if (
    !auth.token ||
    !auth.user ||
    auth.user.role !== "admin" ||
    auth.user.status !== "active"
  )
    throw new ApiError(
      "ADMIN_BEARER_REQUIRED",
      "请重新登录以启用管理操作。",
      "",
      403,
    );
  return (
    await client.post<T>(path, body, {
      signal,
      timeout,
      adminIdentity: { userId: auth.user.id, token: auth.token },
    })
  ).data;
}

export interface AuditFilters {
  limit?: number;
  offset?: number;
  actor_id?: string;
  action?: string;
  target_type?: string;
  target_id?: string;
  outcome?: string;
  since?: string;
  until?: string;
}
export interface AuditEvent {
  id: string;
  actor_kind: string;
  actor_label: string;
  actor_user_id: string | null;
  action: string;
  target_type: string;
  target_id: string;
  reason: string;
  request_id: string;
  idempotency_key: string;
  outcome: string;
  changes: Record<string, unknown>;
  request_data: Record<string, unknown>;
  result: Record<string, unknown>;
  created_at: string;
  current_task_status?: TaskStatus | null;
}
export interface CleanupReceipt {
  operation_id: string;
  state: "completed" | "accepted" | "failed_or_partial" | "outcome_unrecorded";
  audit_id: string;
  duplicate?: boolean;
  requires_review?: boolean;
  error_code?: string;
  files?: number;
  temporary_directories?: number;
  orphan_files?: number;
  freed_bytes?: number;
  used_bytes?: number;
}
