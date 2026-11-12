import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { adminApi, type AdminUser, type GrantProduct } from "./api";
import { ErrorState, formatDateTime, Loading } from "./ui";
import { useOperation, validReason } from "./operations";
import {
  OperationDialog,
  OperationState,
  ReasonFields,
  useWriteAccess,
  WriteLoginNotice,
} from "./OperationDialog";
function GrantForm({ id }: { id: string }) {
  const operation = useOperation("grant", id);
  const [product, setProduct] = useState<GrantProduct>("plus_monthly");
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => {
    if (operation.record) {
      setConfirmed(false);
      setReason("");
    }
  }, [operation.record?.body.key]);
  const user = useQuery({
    queryKey: ["admin", "user", id],
    queryFn: ({ signal }) => adminApi.user(id, signal),
    enabled: !operation.record,
    staleTime: 0,
    refetchOnMount: "always",
  });
  const eligible =
    user.data?.status === "active" && Boolean(user.data.last_login_at);
  const preview = useQuery({
    queryKey: ["admin", "grant-preview", id, product],
    queryFn: ({ signal }) => adminApi.grantPreview(id, product, signal),
    enabled: !operation.record && eligible && !user.isError && !user.isFetching,
    staleTime: 0,
    refetchOnMount: "always",
  });
  if (operation.record)
    return (
      <OperationState kind="grant" target={id} newActionLabel="开始新的发放" />
    );
  if (user.isPending) return <Loading label="正在核对目标账号…" />;
  if (user.isError)
    return <ErrorState error={user.error} retry={() => void user.refetch()} />;
  if (!eligible)
    return <p>仅能向已登录绑定的正常账号发放权益，请先核对账号状态。</p>;
  const valid =
    preview.data?.product === product &&
    preview.data.mode === "operator_grant" &&
    preview.data.payment_verified === false &&
    typeof preview.data.offer?.name === "string" &&
    Number.isSafeInteger(preview.data.offer.asr_seconds) &&
    preview.data.offer.asr_seconds >= 0 &&
    Number.isSafeInteger(preview.data.offer.ai_generations) &&
    preview.data.offer.ai_generations >= 0 &&
    Number.isFinite(Date.parse(preview.data.starts_at)) &&
    Number.isFinite(Date.parse(preview.data.expires_at));
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (
          !valid ||
          preview.isFetching ||
          preview.isError ||
          user.isFetching ||
          !confirmed ||
          !validReason(reason)
        )
          return;
        void operation.run({ reason, product }, preview.data);
      }}
    >
      {user.isFetching && <Loading label="正在重新核对账号…" compact />}
      <p className="admin-operation-summary">
        人工发放给：{user.data.nickname}
      </p>
      <p className="admin-footnote">
        这是人工权益发放，不代表已收到付款。提前续费可能从下一个账期生效，预览时间不锁定。
      </p>
      <label className="admin-reason">
        发放产品
        <select
          value={product}
          onChange={(event) => {
            setProduct(event.target.value as GrantProduct);
            setConfirmed(false);
          }}
        >
          <option value="plus_monthly">Plus 月会员</option>
          <option value="pro_monthly">Pro 月会员</option>
          <option value="monthly">旧版月会员</option>
          <option value="asr_120">120 分钟转写包</option>
          <option value="ai_100">100 次 AI 包</option>
        </select>
      </label>
      {preview.isPending ? (
        <Loading label="正在获取发放预览…" />
      ) : preview.isError ? (
        <ErrorState
          error={preview.error}
          retry={() => void preview.refetch()}
        />
      ) : !valid ? (
        <p role="alert">发放预览不完整，请重新加载后核对。</p>
      ) : (
        <>
          {preview.isFetching && <Loading label="正在更新发放预览…" compact />}
          <p className="admin-footnote">
            有效会员按原到期时间顺延，所选等级在下方预计生效时间开始。
          </p>
          <dl className="admin-details admin-grant-preview">
            <dt>权益内容</dt>
            <dd>
              {preview.data!.offer.name} · 转写{" "}
              {preview.data!.offer.asr_seconds} 秒 / AI{" "}
              {preview.data!.offer.ai_generations} 次
            </dd>
            <dt>预计生效</dt>
            <dd>{formatDateTime(preview.data!.starts_at)}</dd>
            <dt>预计到期</dt>
            <dd>{formatDateTime(preview.data!.expires_at)}</dd>
            <dt>原会员到期</dt>
            <dd>{formatDateTime(preview.data!.member_until_before)}</dd>
          </dl>
        </>
      )}
      <ReasonFields
        reason={reason}
        onReason={setReason}
        confirmed={confirmed}
        onConfirmed={setConfirmed}
      />
      <button
        className="admin-button admin-button-primary"
        disabled={
          !valid ||
          preview.isError ||
          preview.isFetching ||
          user.isFetching ||
          !confirmed ||
          !validReason(reason)
        }
      >
        确认人工发放
      </button>
    </form>
  );
}
export default function GrantAction({ user }: { user: AdminUser }) {
  const access = useWriteAccess();
  const { record } = useOperation("grant", user.id);
  if (!access) return <WriteLoginNotice />;
  if ((user.status !== "active" || !user.last_login_at) && !record)
    return (
      <p className="admin-footnote">
        当前账号不满足发放条件，需要已登录绑定的正常账号。
      </p>
    );
  return (
    <OperationDialog
      kind="grant"
      target={user.id}
      title="人工权益发放"
      trigger={record ? "查看发放操作" : "人工发放权益"}
    >
      <GrantForm id={user.id} />
    </OperationDialog>
  );
}
