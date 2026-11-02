import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, resourceAccessUrl } from "../../api/client";
import { authStore, useAuth } from "../../stores/auth";
import { clipError } from "../../lib/clips";
import type { useClipDelivery } from "../../hooks/useClipDelivery";
import type { ClipExportItem, ClipPlan } from "../../types/clips";
import { filesize, timecode } from "../../utils";
import ErrorNotice from "../ErrorNotice";
import Icon from "../Icon";

function exactTime(seconds: number) {
  const milliseconds = Math.round(seconds * 1000);
  return `${timecode(Math.floor(milliseconds / 1000))}.${String(milliseconds % 1000).padStart(3, "0")}`;
}
export default function ClipDeliveryCards({
  result,
  plan,
  videoKey,
  active,
}: {
  result: ReturnType<typeof useClipDelivery>;
  plan: ClipPlan;
  videoKey: string;
  active: boolean;
}) {
  const auth = useAuth();
  const [clock, setClock] = useState(Date.now());
  const [busy, setBusy] = useState<string>();
  const [notice, setNotice] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const request = useRef<AbortController | null>(null);
  const owner = JSON.stringify([
    auth.user?.id,
    auth.token,
    plan.id,
    result.delivery?.id,
    active,
  ]);
  const currentOwner = useRef(owner);
  currentOwner.current = owner;
  useEffect(() => {
    setBusy(undefined);
    setNotice("");
    setError(null);
    return () => {
      request.current?.abort();
      request.current = null;
    };
  }, [owner]);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  async function download(item: ClipExportItem) {
    if (!active || request.current || !authStore.requireLogin()) return;
    const expected = owner,
      userId = auth.user?.id,
      token = auth.token;
    const controller = new AbortController();
    request.current = controller;
    setBusy(item.file_id);
    setNotice("");
    setError(null);
    const valid = () =>
      !controller.signal.aborted &&
      currentOwner.current === expected &&
      authStore.getState().user?.id === userId &&
      authStore.getState().token === token;
    try {
      const checked = await result.check(controller.signal);
      if (!valid()) return;
      const current = checked.items.find(
        (value) =>
          value.file_id === item.file_id &&
          value.candidate_id === item.candidate_id,
      );
      if (
        !current?.download_available ||
        Date.parse(current.expires_at) <= Date.now()
      )
        throw new ApiError(
          "FILE_EXPIRED",
          "文件已到期或不可用，请重新获取生成报价。",
        );
      const url = await resourceAccessUrl(
        `/files/${encodeURIComponent(current.file_id)}/download`,
        controller.signal,
      );
      if (!valid()) return;
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "";
      anchor.referrerPolicy = "no-referrer";
      anchor.click();
      setNotice("已请求下载，请查看浏览器下载列表。");
    } catch (cause) {
      if (valid()) setError(clipError(cause));
    } finally {
      if (request.current === controller) {
        request.current = null;
        if (valid()) setBusy(undefined);
      }
    }
  }
  const delivery = result.delivery;
  return (
    <section className="clip-delivery" aria-label="视频片段成品">
      <ErrorNotice error={result.error}>
        <button className="text-button" onClick={result.reload}>
          重新加载成品
        </button>
      </ErrorNotice>
      {result.loading && (
        <p className="clip-loading" role="status">
          正在核验成片状态…
        </p>
      )}
      {delivery && (
        <>
          <header className="clip-delivery-heading">
            <div>
              <h3>视频片段</h3>
              <p>逐条下载，文件保留时间各自计算。</p>
            </div>
            <button
              className="text-button"
              onClick={result.reload}
              disabled={result.loading}
            >
              刷新文件状态
            </button>
          </header>
          {result.latest?.snapshot?.result_payload?.cache_hit &&
            result.latest.snapshot.result_payload.clip_export_id ===
              delivery.id && (
              <p className="clip-note" role="status">
                已复用已有视频，文件保留原到期时间。
              </p>
            )}
          <ErrorNotice error={error} />
          <p role="status" className="clip-download-notice">
            {notice}
          </p>
          <div className="clip-files" role="list" aria-label="已生成的视频片段">
            {delivery.items.map((item) => {
              const candidate = plan.candidates.find(
                (value) => value.id === item.candidate_id,
              )!;
              const expired = Date.parse(item.expires_at) <= clock;
              const available = item.download_available && !expired;
              return (
                <article
                  className="clip-file"
                  key={item.file_id}
                  role="listitem"
                >
                  <div>
                    <h4>{candidate.title}</h4>
                    <p className="clip-time">
                      源视频 {exactTime(item.start)}–{exactTime(item.end)}
                    </p>
                    <p className="clip-file-meta">
                      {item.verification
                        ? `${item.verification.duration.toFixed(1)} 秒`
                        : `区间时长 ${(item.end - item.start).toFixed(1)} 秒`}{" "}
                      ·{" "}
                      {item.size_bytes == null
                        ? "文件大小未提供"
                        : filesize(item.size_bytes)}
                    </p>
                    <p className="clip-file-expiry">
                      {expired
                        ? "已到期"
                        : !item.download_available
                          ? "文件不可用"
                          : "可下载"}{" "}
                      · 保留至{" "}
                      <time dateTime={item.expires_at}>
                        {new Intl.DateTimeFormat("zh-CN", {
                          timeZone: "Asia/Shanghai",
                          month: "numeric",
                          day: "numeric",
                          hour: "2-digit",
                          minute: "2-digit",
                        }).format(new Date(item.expires_at))}
                        （北京时间）
                      </time>
                    </p>
                  </div>
                  <button
                    className="button secondary"
                    disabled={!active || !available || !!busy}
                    onClick={() => void download(item)}
                    aria-label={`下载视频：${candidate.title}`}
                  >
                    <Icon name="download" size={15} />
                    {busy === item.file_id ? "准备下载…" : "下载视频"}
                  </button>
                </article>
              );
            })}
          </div>
          <p className="clip-batch-link">
            <Link
              className="text-button"
              to={`/video/${encodeURIComponent(videoKey)}?${new URLSearchParams({ tab: "clips", clip_export: delivery.id })}`}
            >
              打开此批次链接
            </Link>
            <span>其他批次可从任务历史打开。</span>
          </p>
        </>
      )}
    </section>
  );
}
