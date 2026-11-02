import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Icon from "../components/Icon";
import { useQuery } from "@tanstack/react-query";
import { adminApi } from "./api";
import { bytes, ErrorState, Loading, Panel } from "./ui";
import { PageHeading } from "./list";
import { adminKeys, ServiceHealth } from "./OverviewPage";
import { useOperation, validReason } from "./operations";
import {
  OperationDialog,
  OperationState,
  ReasonFields,
  useWriteAccess,
  WriteLoginNotice,
} from "./OperationDialog";
function CleanupForm() {
  const operation = useOperation("cleanup", "local");
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => {
    if (operation.record) {
      setReason("");
      setConfirmed(false);
    }
  }, [operation.record?.body.key]);
  if (operation.record)
    return (
      <OperationState
        kind="cleanup"
        target="local"
        newActionLabel="核对后开始新的清理"
      />
    );
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (confirmed && validReason(reason)) void operation.run({ reason });
      }}
    >
      <p className="admin-operation-summary">
        清理过期文件和可安全移除的临时文件
      </p>
      <p className="admin-footnote">
        有效文件、运行进程组和活任务受后端安全机制保护。文件删除不能回滚；失败可能已处理部分文件，请核查审计后再决定下一步。
      </p>
      <ReasonFields
        reason={reason}
        onReason={setReason}
        confirmed={confirmed}
        onConfirmed={setConfirmed}
      />
      <button
        className="admin-button admin-button-primary"
        disabled={!confirmed || !validReason(reason)}
      >
        确认清理过期文件
      </button>
    </form>
  );
}
function CleanupAction() {
  const access = useWriteAccess();
  const { record } = useOperation("cleanup", "local");
  return access ? (
    <OperationDialog
      kind="cleanup"
      title="清理过期文件"
      target="local"
      trigger={record ? "查看清理操作" : "清理过期文件"}
    >
      <CleanupForm />
    </OperationDialog>
  ) : (
    <WriteLoginNotice />
  );
}
export default function StoragePage() {
  const query = useQuery({
    queryKey: adminKeys.storage,
    queryFn: ({ signal }) => adminApi.storage(signal),
    staleTime: 60000,
    refetchOnWindowFocus: false,
  });
  const data = query.data;
  const complete = data?.scan_complete && data.used_bytes != null;
  return (
    <>
      <PageHeading
        title="存储与服务"
        description="检查 API 与依赖就绪情况，按需查看存储和执行过期文件清理。"
      />
      <div className="admin-grid">
        <Panel
          title="存储空间"
          description="仅进入或手动刷新时扫描，不轮询整个磁盘。"
          busy={query.isFetching}
          action={
            <button
              className="admin-button"
              disabled={query.isFetching}
              onClick={() => void query.refetch()}
              aria-busy={query.isFetching}
            >
              {query.isFetching && (
                <span className="admin-spinner" aria-hidden="true" />
              )}
              刷新存储
            </button>
          }
        >
          {query.isPending ? (
            <Loading />
          ) : query.isError ? (
            <ErrorState
              error={query.error}
              retry={() => void query.refetch()}
            />
          ) : (
            data && (
              <>
                <strong className="admin-quota-value">
                  {bytes(data.used_bytes)}{" "}
                  <small>/ {bytes(data.quota_bytes)}</small>
                </strong>
                {complete ? (
                  <div className="admin-storage">
                    <progress
                      aria-label="存储配额占用"
                      max={Math.max(1, data.quota_bytes)}
                      value={data.used_bytes!}
                    />
                  </div>
                ) : (
                  <p className="admin-footnote">
                    扫描未完成，仅观察到 {bytes(data.observed_bytes)}
                    ；总占用未知，不能计算占用比例。
                  </p>
                )}
                <dl className="admin-details admin-storage-details">
                  <dt>磁盘可用</dt>
                  <dd>{bytes(data.disk_free_bytes)}</dd>
                  <dt>最低保留空间</dt>
                  <dd>{bytes(data.min_free_bytes)}</dd>
                  <dt>文件保留时间</dt>
                  <dd>{data.file_ttl_hours} 小时</dd>
                  <dt>扫描状态</dt>
                  <dd>{complete ? "完整" : "不完整"}</dd>
                </dl>
                {data.disk_free_bytes != null &&
                  data.disk_free_bytes < data.min_free_bytes && (
                    <p className="admin-form-error" role="alert">
                      磁盘可用空间低于安全阈值，请检查存储。
                    </p>
                  )}
                <div className="admin-detail-subtitle">
                  <CleanupAction />
                </div>
              </>
            )
          )}
        </Panel>
        <ServiceHealth showStorage={false} />
      </div>
      <Panel
        title="清理与审计说明"
        className="admin-guide"
        action={
          <Link
            className="admin-text-button"
            to="/admin/audit?target_type=storage&target_id=local"
          >
            查看清理审计 →
          </Link>
        }
      >
        <ol>
          <li>
            <span>1</span>
            <div>
              <strong>记录清理请求</strong>
              <p>填写原因并核对影响，保留原操作编号。</p>
            </div>
          </li>
          <li>
            <span>2</span>
            <div>
              <strong>执行安全清理</strong>
              <p>仅移除过期或可安全清理的数据，活任务受保护。</p>
            </div>
          </li>
          <li>
            <span>3</span>
            <div>
              <strong>核对结果回执</strong>
              <p>检查释放空间和审计；超时后核对原请求。</p>
            </div>
          </li>
        </ol>
        <p className="admin-footnote">
          清理先记录请求，再执行文件操作，最后记录结果。请求超时不能证明文件未被删除；请保留操作编号核查原清理结果。当前
          Worker 存活未监测，健康页不会用绿色状态推断 Worker 正常。
        </p>
      </Panel>
    </>
  );
}
