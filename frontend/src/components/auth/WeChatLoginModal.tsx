import { useEffect, useRef, useState } from "react";
import { authApi, authStore, type LoginSession } from "../../stores/auth";
import { normalizeError } from "../../api/client";
import { useWorkspace } from "../../hooks/useWorkspace";

export default function WeChatLoginModal() {
  const dialog = useRef<HTMLDialogElement>(null);
  const workspace = useWorkspace();
  const [session, setSession] = useState<LoginSession>();
  const [remaining, setRemaining] = useState(300);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let countdown: ReturnType<typeof setInterval> | undefined;
    let expiresAt = 0;
    setSession(undefined);
    setError("");
    setCopied(false);
    async function poll(id: string) {
      if (controller.signal.aborted || Date.now() >= expiresAt) return;
      try {
        const result = await authApi.poll(id, controller.signal);
        if (controller.signal.aborted) return;
        if (result.status === "authenticated") {
          authStore.accept(result);
          workspace.switchUser(result.user!.id);
          try {
            await workspace.loadHistory();
          } catch {
            /* History can be retried separately; credentials are already saved. */
          }
          if (!controller.signal.aborted) authStore.close();
          return;
        }
        if (result.status === "expired") {
          setRemaining(0);
          return;
        }
        setError("");
      } catch (cause) {
        if (controller.signal.aborted) return;
        setError(normalizeError(cause).message);
      }
      timer = setTimeout(() => void poll(id), 1500);
    }
    void authApi
      .create(controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setSession(value);
        expiresAt = Date.now() + value.expires_in * 1000;
        setRemaining(value.expires_in);
        countdown = setInterval(() => {
          const left = Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
          setRemaining(left);
          if (!left && countdown) clearInterval(countdown);
        }, 1000);
        timer = setTimeout(() => void poll(value.scene_id), 1500);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(normalizeError(cause).message);
      });
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
      if (countdown) clearInterval(countdown);
      dialog.current?.close();
      previous?.focus();
    };
  }, [refresh, workspace]);
  async function copy() {
    try {
      await navigator.clipboard.writeText(session!.verify_code);
      setCopied(true);
    } catch {
      setError("复制失败，请手动复制上方口令。");
    }
  }
  async function confirm() {
    if (!session) return;
    setBusy(true);
    setError("");
    try {
      await authApi.confirm(session.scene_id);
    } catch (cause) {
      setError(normalizeError(cause).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="wechat-modal"
      aria-labelledby="wechat-login-title"
      onCancel={(event) => {
        event.preventDefault();
        authStore.close();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) authStore.close();
      }}
    >
      <div className="wechat-content">
        <button
          className="wechat-close"
          aria-label="关闭登录弹窗"
          onClick={authStore.close}
        >
          ×
        </button>
        <p className="wechat-eyebrow">VIDEOHUB / 微信登录</p>
        <h2 id="wechat-login-title">关注，即刻开始。</h2>
        <p className="muted">在公众号发送口令，安全连接你的工作台。</p>
        {session ? (
          <>
            <img
              className="wechat-qr"
              src={session.qr_url}
              alt="微信公众号二维码"
            />
            {session.mode === "mock" && (
              <p className="muted">本地演示模式 · 使用下方模拟按钮完成登录</p>
            )}
            <div className="wechat-code-card">
              <span>本次登录口令</span>
              <strong aria-label="六位登录口令">{session.verify_code}</strong>
              <button onClick={() => void copy()}>
                {copied ? "已复制" : "一键复制口令"}
              </button>
            </div>
            <ol className="wechat-steps">
              <li>第一步：微信扫码关注公众号</li>
              <li>
                第二步：在公众号聊天框发送上方 6 位数字口令，网页将自动完成登录
              </li>
            </ol>
            <p className="wechat-expiry" role="status">
              {remaining > 0
                ? `${remaining} 秒后失效`
                : "口令已过期，请刷新后重试"}
            </p>
            {remaining > 0 && session.mode === "mock" && (
              <button
                className="wechat-mock"
                disabled={busy}
                onClick={() => void confirm()}
              >
                {busy ? "正在确认…" : "⚡ 本地模拟关注并发送口令"}
              </button>
            )}
          </>
        ) : (
          !error && <p role="status">正在生成登录口令…</p>
        )}
        {error && (
          <p role="alert" className="wechat-error">
            {error}
          </p>
        )}
        {(remaining === 0 || error) && (
          <button onClick={() => setRefresh((value) => value + 1)}>
            刷新登录二维码与口令
          </button>
        )}
      </div>
    </dialog>
  );
}
