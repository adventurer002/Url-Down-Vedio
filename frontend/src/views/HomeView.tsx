import { useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { ApiError, normalizeError } from "../api/client";
import { useWorkspace } from "../hooks/useWorkspace";
import ErrorNotice from "../components/ErrorNotice";
import Icon from "../components/Icon";
const features = [
  {
    n: "01",
    icon: "download",
    title: "视频下载",
    description: "选择合适的清晰度，让精彩随时可看。",
    note: "原生档位 · 本地保存",
  },
  {
    n: "02",
    icon: "text",
    title: "音频与文稿",
    description: "留下声音与文字，换一种方式吸收内容。",
    note: "纯音频提取 · 字幕优先",
  },
  {
    n: "03",
    icon: "map",
    title: "AI 总结与导图",
    description: "从长视频中提炼重点，让知识脉络清晰可见。",
    note: "章节摘要 · 交互导图",
  },
];
export default function HomeView() {
  const [url, setUrl] = useState(""),
    [loading, setLoading] = useState(false),
    [error, setError] = useState<ApiError | null>(null);
  const pending = useRef(false),
    navigate = useNavigate(),
    store = useWorkspace();
  async function parse(event?: FormEvent) {
    event?.preventDefault();
    if (pending.current) return;
    setError(null);
    const input = url.trim();
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
        <div className="hero-kicker">
          <span className="tiny-square" /> YOUR VIDEO. YOUR KNOWLEDGE.
        </div>
        <h1>
          下载视频，
          <br className="mobile-break" />
          <span>理解内容。</span>
        </h1>
        <p className="hero-description">
          一个链接，连接视频、文稿与灵感。
          <br className="mobile-break" />
          让每一次观看，都有所收获。
        </p>
        <form className="parse-form" onSubmit={parse} aria-busy={loading}>
          <label className="sr-only" htmlFor="video-url">
            视频链接
          </label>
          <div className="url-field">
            <Icon name="link" size={22} />
            <input
              id="video-url"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              type="url"
              required
              maxLength={8192}
              placeholder="粘贴公开的视频链接…"
              disabled={loading}
              autoComplete="url"
              aria-invalid={!!error}
            />
            <button className="button primary" disabled={loading} type="submit">
              {loading && <span className="spinner" />}
              {loading ? "正在解析" : "开始解析"}
              {!loading && <Icon name="arrow" size={18} />}
            </button>
          </div>
          <ErrorNotice error={error}>
            <button
              className="text-button"
              type="button"
              disabled={loading}
              onClick={() => void parse()}
            >
              保留链接，重新尝试 ↗
            </button>
          </ErrorNotice>
        </form>
        <div className="hero-footnote">
          <span className="mini-dot" />
          仅支持公开免登录资源<span className="footnote-separator">/</span>
          所有操作，尽在一个工作台
        </div>
      </section>
      <section className="feature-section" aria-label="功能介绍">
        <div className="section-caption">
          <span>更少操作，更多收获</span>
          <span>THREE WAYS TO GO FURTHER</span>
        </div>
        <div className="feature-grid">
          {features.map((feature) => (
            <article key={feature.n} className="feature-card">
              <div className="feature-top">
                <Icon name={feature.icon} size={26} />
                <span>{feature.n}</span>
              </div>
              <h2>{feature.title}</h2>
              <p>{feature.description}</p>
              <div className="feature-note">
                {feature.note}
                <Icon name="arrow" size={16} />
              </div>
            </article>
          ))}
        </div>
      </section>
      <div className="home-bottom">
        <span>为专注而设计。</span>
        <span>从第一帧，到最后一个想法。</span>
      </div>
    </div>
  );
}
