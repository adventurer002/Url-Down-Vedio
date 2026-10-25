import { useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { authStore, useAuth } from "../stores/auth";
import { ApiError, normalizeError } from "../api/client";
import { useWorkspace } from "../hooks/useWorkspace";
import ErrorNotice from "../components/ErrorNotice";
import Icon from "../components/Icon";
import PlatformLogos from "../components/PlatformLogos";
import HeroBackground from "../components/HeroBackground";
export default function HomeView() {
  useAuth();
  const signedIn = authStore.hasAccess();
  const [url, setUrl] = useState(""),
    [loading, setLoading] = useState(false),
    [error, setError] = useState<ApiError | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const pending = useRef(false),
    navigate = useNavigate(),
    store = useWorkspace();
  async function parse(event?: FormEvent) {
    event?.preventDefault();
    if (!authStore.requireLogin()) return;
    if (pending.current) return;
    setError(null);
    const input = url.trim();
    if (!input) {
      setError(new ApiError("VIDEO_URL_REQUIRED", "请先粘贴视频链接"));
      inputRef.current?.focus();
      return;
    }
    try {
      const parsed = new URL(input);
      if (!["http:", "https:"].includes(parsed.protocol)) throw new Error();
    } catch {
      setError(
        new ApiError("INVALID_URL", "请输入完整的 http 或 https 视频链接。"),
      );
      return;
    }
    pending.current = true;
    setLoading(true);
    try {
      const metadata = await api.parse(input);
      navigate("/video/" + encodeURIComponent(store.remember(input, metadata)));
    } catch (e) {
      setError(normalizeError(e));
    } finally {
      pending.current = false;
      setLoading(false);
    }
  }
  return (
    <div className="home-page">
      <section className="hero">
        <HeroBackground />
        <div className="hero-content">
          <div className="hero-kicker">
            <span className="tiny-square" /> YOUR VIDEO. YOUR KNOWLEDGE.
          </div>
          <h1>
            下载视频，
            <br className="mobile-break" />
            <span>理解内容。</span>
          </h1>
          <p className="hero-description">
            粘贴视频链接，下载视频、提取文稿，生成 AI 总结与思维导图。
            <br className="mobile-break" />
            让每一次观看，都有所收获。
          </p>
          <form
            className="parse-form"
            onSubmit={parse}
            aria-busy={loading}
            noValidate
          >
            <label className="sr-only" htmlFor="video-url">
              视频链接
            </label>
            <div className="url-field">
              <Icon name="link" size={22} />
              <input
                ref={inputRef}
                id="video-url"
                value={url}
                readOnly={!signedIn}
                onClick={() => {
                  authStore.requireLogin();
                }}
                onKeyDown={(event) => {
                  if (
                    !signedIn &&
                    (event.key.length === 1 || event.key === "Enter")
                  ) {
                    event.preventDefault();
                    authStore.requireLogin();
                  }
                }}
                onPaste={(event) => {
                  if (!authStore.requireLogin()) event.preventDefault();
                }}
                onChange={(event) => {
                  if (!authStore.requireLogin()) return;
                  setUrl(event.target.value);
                  if (error) setError(null);
                }}
                type="url"
                required
                maxLength={8192}
                placeholder="粘贴公开的视频链接…"
                disabled={loading}
                autoComplete="url"
                aria-invalid={!!error}
                aria-describedby={error ? "video-url-error" : undefined}
              />
              <button
                className="button primary"
                disabled={loading}
                type="submit"
              >
                {loading && <span className="spinner" />}
                {loading ? "正在解析" : "开始解析"}
                {!loading && <Icon name="arrow" size={18} />}
              </button>
            </div>
            <div id="video-url-error">
              <ErrorNotice
                error={error?.code === "VIDEO_URL_REQUIRED" ? null : error}
              >
                <button
                  className="text-button"
                  type="button"
                  disabled={loading}
                  onClick={() => void parse()}
                >
                  保留链接，重新尝试 ↗
                </button>
              </ErrorNotice>
              {error?.code === "VIDEO_URL_REQUIRED" && (
                <p className="input-hint" role="alert">
                  请先粘贴视频链接
                </p>
              )}
            </div>
          </form>
          <div className="hero-footnote">
            <span className="mini-dot" />
            仅支持公开免登录资源<span className="footnote-separator">/</span>
            所有操作，尽在一个工作台
          </div>
        </div>
      </section>
      <PlatformLogos />
    </div>
  );
}
