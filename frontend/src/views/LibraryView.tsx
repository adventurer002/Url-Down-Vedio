import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { normalizeError, type ApiError } from "../api/client";
import { useAuth, authStore } from "../stores/auth";
import { useWorkspace } from "../hooks/useWorkspace";
import type { LibraryPage, LibraryDetail } from "../types/knowledge";
import type { SavedVideo } from "../stores/workspace";
import ErrorNotice from "../components/ErrorNotice";
import Icon from "../components/Icon";
import SaveToLibrary from "../components/SaveToLibrary";
import VideoQuestions from "../components/VideoQuestions";
import { dateLabel } from "../utils/billing";

export default function LibraryView() {
  const { user } = useAuth();
  return (
    <div className="library-page">
      <Link className="back-link" to="/">
        ← 返回首页
      </Link>
      <header className="library-heading">
        <span className="eyebrow">YOUR KNOWLEDGE, CONNECTED</span>
        <h1>看过的，留下来。</h1>
        <p>收藏视频文稿和总结，让每一次观看都有迹可循。</p>
      </header>
      {user ? (
        <LibrarySpace key={user.id} userId={user.id} />
      ) : (
        <div className="library-empty">
          <Icon name="bookmark" size={32} />
          <h2>给好内容一个自己的位置</h2>
          <p>登录后查看你的收藏。收藏、搜索与再次查看免费。</p>
          <button className="button primary" onClick={authStore.open}>
            登录查看知识库
          </button>
        </div>
      )}
    </div>
  );
}
function LibrarySpace({ userId }: { userId: string }) {
  const store = useWorkspace();
  const [input, setInput] = useState(""),
    [query, setQuery] = useState(""),
    [tag, setTag] = useState(""),
    [offset, setOffset] = useState(0),
    [attempt, setAttempt] = useState(0);
  const [page, setPage] = useState<LibraryPage>(),
    [detail, setDetail] = useState<LibraryDetail>(),
    [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(false),
    [detailLoading, setDetailLoading] = useState(false),
    [pending, setPending] = useState(false),
    [error, setError] = useState<ApiError | null>(null);
  const alive = useRef(true),
    reader = useRef<HTMLElement>(null);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void api
      .library(query, tag, offset, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setPage(data);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(normalizeError(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [query, tag, offset, attempt]);
  useEffect(() => {
    setDetail(undefined);
    if (!selected) return;
    const controller = new AbortController();
    setDetailLoading(true);
    setError(null);
    void api
      .libraryItem(selected, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setDetail(data);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(normalizeError(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [selected, attempt]);
  useEffect(() => {
    if (detail)
      reader.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [detail?.id]);
  function savedVideo(item: LibraryDetail): SavedVideo {
    return {
      key: `${item.video.platform}_${item.video.platform_video_id}`,
      url: item.video.source_url,
      metadata: {
        ...item.video,
        has_subtitles: item.source_type === "subtitle",
        download_options: [],
      },
      partial: true,
    };
  }
  async function remove() {
    if (pending || !detail) return;
    setPending(true);
    setError(null);
    try {
      await api.unsaveVideo(detail.id);
      if (alive.current && authStore.getState().user?.id === userId) {
        setSelected("");
        setDetail(undefined);
        setOffset(0);
        setAttempt((value) => value + 1);
      }
    } catch (cause) {
      if (alive.current) setError(normalizeError(cause));
    } finally {
      if (alive.current) setPending(false);
    }
  }
  return (
    <>
      <form
        className="library-search"
        onSubmit={(event) => {
          event.preventDefault();
          setQuery(input.trim());
          setOffset(0);
        }}
      >
        <Icon name="search" />
        <input
          aria-label="搜索知识库"
          maxLength={200}
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder="搜索标题、文稿、总结或标签"
        />
        <button className="button primary">搜索</button>
      </form>
      <div className="library-list-heading">
        <span>
          {page ? `${page.total} 条收藏` : "我的收藏"} · 搜索与阅读免费
        </span>
        {tag && (
          <button
            className="text-button"
            onClick={() => {
              setTag("");
              setOffset(0);
            }}
          >
            标签：{tag} ×
          </button>
        )}
      </div>
      <ErrorNotice error={error}>
        <button
          className="text-button"
          onClick={() => setAttempt((value) => value + 1)}
        >
          重新加载知识库
        </button>
      </ErrorNotice>
      {loading && (
        <p className="muted" role="status">
          正在读取收藏…
        </p>
      )}
      {!loading && page?.items.length === 0 && (
        <div className="library-empty">
          <Icon name="bookmark" size={32} />
          <h2>{query || tag ? "暂时没有匹配的内容" : "从第一条收藏开始"}</h2>
          <p>
            {query || tag
              ? "换一个关键词，或清除标签再试。"
              : "在视频工作台获取文稿后，点击“收藏到知识库”。"}
          </p>
          <Link className="button secondary" to="/">
            去探索视频
          </Link>
        </div>
      )}
      <div className="library-cards">
        {page?.items.map((item) => (
          <article
            className={
              "library-card" + (selected === item.id ? " selected" : "")
            }
            key={item.id}
          >
            <div className="library-card-top">
              <span>
                {item.video.platform} ·{" "}
                {item.source_type === "asr" ? "语音转写" : "平台字幕"}
              </span>
              <time>{dateLabel(item.updated_at)}</time>
            </div>
            <h2>
              <button onClick={() => setSelected(item.id)}>{item.title}</button>
            </h2>
            <p>{item.excerpt}</p>
            <div className="library-tags">
              {item.tags.map((value) => (
                <button
                  key={value}
                  onClick={() => {
                    setTag(value);
                    setOffset(0);
                  }}
                >
                  #{value}
                </button>
              ))}
            </div>
            <button
              className="text-button"
              onClick={() => setSelected(item.id)}
            >
              阅读与提问 <Icon name="arrow" size={14} />
            </button>
          </article>
        ))}
      </div>
      {page && page.total > page.limit && (
        <nav className="library-pagination" aria-label="知识库分页">
          <button
            className="button secondary"
            disabled={loading || offset === 0}
            onClick={() =>
              setOffset((value) => Math.max(0, value - page.limit))
            }
          >
            上一页
          </button>
          <span>
            {Math.floor(offset / page.limit) + 1} /{" "}
            {Math.ceil(page.total / page.limit)}
          </span>
          <button
            className="button secondary"
            disabled={loading || offset + page.limit >= page.total}
            onClick={() => setOffset((value) => value + page.limit)}
          >
            下一页
          </button>
        </nav>
      )}
      {detailLoading && <p role="status">正在打开收藏…</p>}
      {detail && (
        <section className="library-reader" ref={reader} aria-label="收藏内容">
          <div className="library-reader-heading">
            <div>
              <span className="eyebrow">SAVED FOR YOU</span>
              <h2>{detail.title}</h2>
            </div>
            <button className="text-button" onClick={() => setSelected("")}>
              收起 <Icon name="close" size={16} />
            </button>
          </div>
          <div className="library-reader-actions">
            <SaveToLibrary
              transcriptId={detail.transcript_id}
              resultId={detail.result_id || undefined}
              onSaved={() => setAttempt((value) => value + 1)}
            />
            <button
              className="text-button"
              disabled={pending}
              onClick={() => void remove()}
            >
              {pending ? "正在移除…" : "移出收藏"}
            </button>
            <Link
              to={"/video/" + savedVideo(detail).key}
              onClick={() => {
                const video = savedVideo(detail);
                store.remember(video.url, video.metadata, true);
              }}
            >
              打开视频工作台 →
            </Link>
          </div>
          {detail.summary_text && (
            <div className="saved-reading">
              <h3>保存的总结</h3>
              <p>{detail.summary_text}</p>
            </div>
          )}
          <details className="saved-transcript">
            <summary>查看原始文稿</summary>
            <p>{detail.content_text}</p>
          </details>
          <VideoQuestions
            video={savedVideo(detail)}
            transcriptId={detail.transcript_id}
          />
        </section>
      )}
    </>
  );
}
