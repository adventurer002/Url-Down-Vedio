import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Navigate } from "react-router-dom";

import { ApiRequestError, apiFetch } from "../api/client";
import { ErrorNote } from "../components/VideoCard";
import { useAuthStore } from "../stores/auth";
import { useMe } from "../hooks/useVideos";

interface Page<T> {
  items: T[];
  total: number;
}

async function adminGet<T>(path: string): Promise<Page<T>> {
  return apiFetch(`${path}`);
}

async function adminPost(path: string): Promise<Record<string, string>> {
  return apiFetch(path, { method: "POST" });
}

export function AdminPage() {
  const loggedIn = useAuthStore((s) => !!s.accessToken);
  const { data: me } = useMe(loggedIn);
  const qc = useQueryClient();
  const [note, setNote] = useState<{ code: string; message: string } | null>(null);
  const [search, setSearch] = useState("");

  if (!loggedIn) return <Navigate to="/login" replace />;

  return (
    <div className="space-y-8 pt-10">
      <section>
        <p className="eyebrow">Admin</p>
        <h1 className="page-title mt-3">管理控制台</h1>
        {me && !me.user.is_admin && (
          <p className="mt-2 text-sm text-muted">当前账号不是管理员，仅管理员可查看数据。</p>
        )}
      </section>
      {note && <ErrorNote code={note.code} message={note.message} />}

      <AdminSection
        title="用户"
        queryKey={["admin-users", search]}
        queryFn={() =>
          adminGet<Record<string, unknown>>(
            `/api/v1/admin/users?page_size=20${search ? `&search=${encodeURIComponent(search)}` : ""}`,
          )
        }
        toolbar={
          <input
            className="field max-w-xs"
            placeholder="按邮箱搜索"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        }
        render={(u) => (
          <div className="list-row">
            <span className="font-mono">{String(u.email)}</span>
            <span className="text-muted">
              {u.is_admin ? "admin" : "user"} · {String(u.nickname)}
            </span>
          </div>
        )}
      />

      <AdminSection
        title="订单"
        queryKey={["admin-orders"]}
        queryFn={() => adminGet<Record<string, unknown>>("/api/v1/admin/orders?page_size=20")}
        render={(o) => (
          <div className="list-row">
            <span className="font-mono">{String(o.order_no)}</span>
            <span className="text-muted">
              {String(o.status)} · {(Number(o.amount_cents) / 100).toFixed(2)}
            </span>
          </div>
        )}
      />

      <UsageSection />

      <CleanupSection
        onDone={() => {
          void qc.invalidateQueries({ queryKey: ["admin-cleanup"] });
        }}
        onError={(code, message) => setNote({ code, message })}
      />

      <AdminSection
        title="审计日志"
        queryKey={["admin-audit"]}
        queryFn={() => adminGet<Record<string, unknown>>("/api/v1/admin/audit-logs?page_size=20")}
        render={(a) => (
          <div className="list-row">
            <span className="font-mono">
              {String(a.action)} · {String(a.resource_id)}
            </span>
            <span className="font-mono text-muted">{String(a.created_at)}</span>
          </div>
        )}
      />
    </div>
  );
}

function AdminSection<T>({
  title,
  queryKey,
  queryFn,
  render,
  toolbar,
}: {
  title: string;
  queryKey: unknown[];
  queryFn: () => Promise<Page<T>>;
  render: (item: T) => React.ReactNode;
  toolbar?: React.ReactNode;
}) {
  const q = useQuery({ queryKey, queryFn, retry: false });
  return (
    <section className="card p-5">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold">{title}</h2>
        {toolbar}
      </div>
      {q.isLoading && <p className="text-sm text-muted">加载中…</p>}
      {q.isError && (
        <p className="text-sm text-muted">
          {q.error instanceof ApiRequestError ? q.error.message : "加载失败（可能无权限）"}
        </p>
      )}
      {q.data && (
        <>
          <p className="mb-2 font-mono text-xs text-muted">共 {q.data.total} 条</p>
          <div>{q.data.items.map((item, i) => (
            <div key={i}>{render(item)}</div>
          ))}</div>
        </>
      )}
    </section>
  );
}

function UsageSection() {
  const q = useQuery({
    queryKey: ["admin-usage"],
    queryFn: () => apiFetch<Record<string, number | string>[]>("/api/v1/admin/usage/summary"),
    retry: false,
  });
  return (
    <section className="card p-5">
      <h2 className="mb-3 text-sm font-semibold">用量汇总</h2>
      {q.data && (
        <div>
          {q.data.map((r) => (
            <div key={String(r.action)} className="list-row">
              <span className="font-mono">{String(r.action)}</span>
              <span className="font-mono text-muted">
                {Number(r.calls)} 次 · in {Number(r.llm_input_tokens)} / out{" "}
                {Number(r.llm_output_tokens)} · asr {Number(r.asr_seconds)}s
              </span>
            </div>
          ))}
          {q.data.length === 0 && <p className="text-muted">暂无用量</p>}
        </div>
      )}
    </section>
  );
}

function CleanupSection({
  onDone,
  onError,
}: {
  onDone: () => void;
  onError: (code: string, message: string) => void;
}) {
  const preview = useQuery({
    queryKey: ["admin-cleanup"],
    queryFn: () => apiFetch<{ expired_pending: number }>("/api/v1/admin/media/cleanup", { method: "POST" }),
    retry: false,
  });
  const revoke = useMutation({
    mutationFn: (id: string) => adminPost(`/api/v1/admin/subscriptions/${id}/revoke`),
    onSuccess: () => onDone(),
    onError: (e) => {
      if (e instanceof ApiRequestError) onError(e.code, e.message);
    },
  });
  const [revokeId, setRevokeId] = useState("");
  return (
    <section className="card space-y-3 p-5">
      <h2 className="text-sm font-semibold">运维</h2>
      <p className="font-mono text-xs text-muted">
        待清理过期文件：{preview.data ? preview.data.expired_pending : "…"}
        （清理执行仍走 Celery Beat 定时任务）
      </p>
      <div className="flex gap-2">
        <input
          className="field max-w-xs"
          placeholder="订阅 ID（撤销）"
          value={revokeId}
          onChange={(e) => setRevokeId(e.target.value)}
        />
        <button
          className="btn btn-secondary"
          disabled={!revokeId || revoke.isPending}
          onClick={() => revoke.mutate(revokeId)}
        >
          撤销订阅
        </button>
      </div>
      {revoke.data && (
        <p className="font-mono text-xs text-muted">已撤销：{revoke.data.subscription_id}</p>
      )}
    </section>
  );
}
