import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";

import { ApiRequestError } from "../api/client";
import { createOrder, formatPrice, getOrder, listPlans } from "../api/videos";
import { ErrorNote } from "../components/VideoCard";

const PROVIDER_LABEL: Record<string, string> = {
  stripe: "国际卡（Stripe）",
  wechat: "微信支付",
  alipay: "支付宝",
};

export function PlansPage() {
  const navigate = useNavigate();
  const [provider, setProvider] = useState("alipay");
  const [note, setNote] = useState<{ code: string; message: string } | null>(null);
  const plansQuery = useQuery({ queryKey: ["plans"], queryFn: listPlans });

  const order = useMutation({
    mutationFn: (planCode: string) => createOrder(planCode, provider),
    onSuccess: (data) => {
      sessionStorage.setItem(`pay:${data.order_no}`, JSON.stringify(data.pay_params));
      navigate(`/pay/${data.order_no}`);
    },
    onError: (e) => {
      if (e instanceof ApiRequestError) setNote({ code: e.code, message: e.message });
    },
  });

  return (
    <div className="space-y-8 pt-10">
      <section className="text-center">
        <p className="eyebrow">Pricing</p>
        <h1 className="mt-3 text-4xl font-normal tracking-[-0.02em]">选择适合你的套餐</h1>
        <p className="mx-auto mt-3 max-w-xl text-muted">
          免费版每日 3 次下载。会员解锁转写、总结、思维导图与更高配额。
        </p>
      </section>

      {note && <ErrorNote code={note.code} message={note.message} />}

      <section className="mx-auto flex max-w-xl items-center justify-center gap-2">
        <span className="label">支付渠道</span>
        {["alipay", "wechat", "stripe"].map((p) => (
          <button
            key={p}
            onClick={() => setProvider(p)}
            className={`seg-item text-sm ${
              provider === p ? "seg-item-active" : "seg-item-idle"
            }`}
          >
            {PROVIDER_LABEL[p]}
          </button>
        ))}
      </section>

      <section className="grid gap-4 sm:grid-cols-3">
        {(plansQuery.data ?? []).map((plan) => (
          <div key={plan.code} className="card flex flex-col p-6">
            <p className="text-sm font-semibold">{plan.name}</p>
            <p className="mt-2 text-3xl font-normal tracking-tight">
              {plan.price_cents === 0 ? "免费" : formatPrice(plan.price_cents, plan.currency)}
            </p>
            <p className="mt-1 font-mono text-xs text-muted">
              {plan.duration_days ? `有效期 ${plan.duration_days} 天` : "永久有效"}
              {" · "}
              每日 {plan.max_daily_downloads} 次
            </p>
            <div className="mt-4 flex-1 space-y-1.5 text-sm text-muted">
              {plan.features.transcribe && <p><span className="text-ok">✓</span> 视频转写</p>}
              {plan.features.summarize && <p><span className="text-ok">✓</span> AI 总结</p>}
              {plan.features.mindmap && <p><span className="text-ok">✓</span> 思维导图</p>}
              {plan.features.audio_extract && <p><span className="text-ok">✓</span> 音频提取</p>}
              {!plan.features.transcribe && <p>— AI 功能需会员</p>}
            </div>
            {plan.price_cents === 0 ? (
              <p className="btn btn-secondary mt-6 w-full">当前方案</p>
            ) : (
              <button
                className="btn btn-primary mt-6 w-full"
                disabled={order.isPending}
                onClick={() => order.mutate(plan.code)}
              >
                {order.isPending ? "下单中…" : "立即订阅"}
              </button>
            )}
          </div>
        ))}
      </section>
      {plansQuery.isLoading && <p className="text-center text-sm text-muted">加载中…</p>}
    </div>
  );
}

export function PayPage() {
  const { orderNo } = useParams();
  const status = useQuery({
    queryKey: ["order", orderNo],
    queryFn: () => getOrder(orderNo as string),
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "pending" ? 2000 : false;
    },
  });

  const order = status.data;
  const payParams = readPayParams(orderNo);

  function readPayParams(
    key: string | undefined,
  ): { provider: string; pay_url?: string | null; qr_code?: string | null } | null {
    try {
      const raw = sessionStorage.getItem(`pay:${key}`);
      if (!raw) return null;
      return JSON.parse(raw) as {
        provider: string;
        pay_url?: string | null;
        qr_code?: string | null;
      };
    } catch {
      return null;
    }
  }

  return (
    <div className="mx-auto max-w-xl space-y-6 pt-10">
      <p className="eyebrow">Payment</p>
      <h1 className="page-title">完成支付</h1>
      {!order && <p className="text-sm text-muted">加载订单…</p>}
      {order && (
        <>
          <div className="card space-y-2 p-5">
            <div className="flex justify-between text-sm">
              <span className="text-muted">订单号</span>
              <span className="font-mono text-xs">{order.order_no}</span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-muted">金额</span>
              <span className="font-semibold">
                {formatPrice(order.amount_cents, order.currency)}
              </span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-muted">状态</span>
              <span className="font-medium">
                {order.status === "paid" ? (
                  <>
                    已支付 <span className="text-ok">✓</span>
                  </>
                ) : order.status === "pending" ? (
                  "等待支付…（自动刷新）"
                ) : (
                  order.status
                )}
              </span>
            </div>
          </div>
          {order.status === "paid" ? (
            <a className="btn btn-primary w-full" href="/">
              支付成功，去使用
            </a>
          ) : (
            <div className="card space-y-3 p-5">
              {payParams?.pay_url && (
                <a
                  className="btn btn-primary w-full"
                  href={payParams.pay_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  前往支付（{PROVIDER_LABEL[payParams.provider] ?? payParams.provider}）
                </a>
              )}
              {payParams?.qr_code && (
                <div className="space-y-2">
                  <p className="text-sm">请用微信扫码支付：</p>
                  <p className="break-all rounded-md bg-faint p-3 font-mono text-xs">
                    {payParams.qr_code}
                  </p>
                </div>
              )}
              {!payParams && (
                <p className="text-sm text-muted">
                  支付参数已过期，请返回套餐页重新下单（订单 30 分钟内有效）。
                </p>
              )}
              <p className="text-xs text-muted">
                支付完成后本页自动跳转。掉单请联系客服补单。
              </p>
            </div>
          )}
        </>
      )}
    </div>
  );
}
