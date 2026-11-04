import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { authStore, useAuth } from "../stores/auth";
import { ApiError, normalizeError } from "../api/client";
import { useWorkspace } from "../hooks/useWorkspace";
import { useBilling } from "../stores/billing";
import { validateFile, isSending } from "../stores/uploads";
import { filesize } from "../utils";
import { extractVideoLink } from "../utils/videoLink";
import ErrorNotice from "../components/ErrorNotice";
import Icon from "../components/Icon";
import HeroBackground from "../components/HeroBackground";
import PlatformLogos from "../components/PlatformLogos";
import "../home.css";

export default function HomeView() {
  const auth = useAuth(),
    billing = useBilling(),
    store = useWorkspace(),
    navigate = useNavigate();
  const [mode, setMode] = useState<"url" | "file">("url"),
    [url, setUrl] = useState("");
  const [file, setFile] = useState<File>(),
    [loading, setLoading] = useState(false),
    [dragging, setDragging] = useState(false);
  const [error, setError] = useState<ApiError | null>(null),
    [adVisible, setAdVisible] = useState(true);
  const inputRef = useRef<HTMLInputElement>(null),
    fileInput = useRef<HTMLInputElement>(null),
    pending = useRef(false);
  const request = useRef<AbortController | null>(null),
    alive = useRef(true),
    previousUser = useRef(auth.user?.id);
  const active = store.uploads.entries.some(isSending);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      request.current?.abort();
    };
  }, []);
  useEffect(() => {
    request.current?.abort();
    pending.current = false;
    setLoading(false);
    if (previousUser.current && previousUser.current !== auth.user?.id) {
      setFile(undefined);
      setError(null);
    }
    previousUser.current = auth.user?.id;
  }, [auth.user?.id]);
  function choose(file?: File) {
    setError(null);
    if (!file) return;
    try {
      validateFile(file, billing.data?.limits.file_bytes);
      setFile(file);
      setMode("file");
    } catch (cause) {
      setError(normalizeError(cause));
    }
  }
  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (pending.current) return;
    setError(null);
    if (mode === "file" && !file) {
      fileInput.current?.click();
      return;
    }
    let input = url.trim();
    if (mode === "url") {
      if (!input) {
        setError(new ApiError("VIDEO_URL_REQUIRED", "请先粘贴视频链接"));
        inputRef.current?.focus();
        return;
      }
      try {
        input = extractVideoLink(input);
      } catch (cause) {
        setError(normalizeError(cause));
        return;
      }
    }
    if (!authStore.requireLogin()) return;
    const controller = new AbortController(),
      userId = authStore.getState().user?.id;
    request.current = controller;
    pending.current = true;
    setLoading(true);
    try {
      let key: string;
      if (mode === "file")
        key = await store.uploads.begin(file!, billing.data?.limits.file_bytes);
      else {
        const metadata = await api.parse(input, controller.signal);
        if (
          !alive.current ||
          controller.signal.aborted ||
          userId !== authStore.getState().user?.id
        )
          return;
        key = store.remember(input, metadata);
      }
      if (
        alive.current &&
        !controller.signal.aborted &&
        userId === authStore.getState().user?.id
      )
        navigate(`/video/${encodeURIComponent(key)}`);
    } catch (cause) {
      if (alive.current && !controller.signal.aborted)
        setError(normalizeError(cause));
    } finally {
      if (alive.current && request.current === controller) {
        request.current = null;
        pending.current = false;
        setLoading(false);
      }
    }
  }
  function changeMode(next: "url" | "file") {
    setMode(next);
    setDragging(false);
    setError(null);
  }
  return (
    <div className="home-page home-import-page">
      <section className="hero" aria-labelledby="home-title">
        <HeroBackground />
        <div className="hero-content">
          <div className="hero-kicker">
            <span className="tiny-square" /> YOUR VIDEO. YOUR KNOWLEDGE.
          </div>
          <h1 id="home-title">
            下载视频，
            <br className="mobile-break" />
            <span>理解内容。</span>
          </h1>
          <p className="hero-description">
            粘贴视频链接，或上传本地视频与音频。
            <br />
            提取文稿，生成 AI 总结与思维导图。
          </p>
          <section className="home-import" aria-label="导入内容">
            <div
              className="home-source-tabs"
              role="tablist"
              aria-label="内容来源"
            >
              {(
                [
                  { id: "url", label: "视频链接", icon: "link" },
                  { id: "file", label: "上传文件", icon: "upload" },
                ] as const
              ).map((item, index) => (
                <button
                  key={item.id}
                  type="button"
                  role="tab"
                  id={`import-tab-${item.id}`}
                  aria-selected={mode === item.id}
                  aria-controls={`import-panel-${item.id}`}
                  tabIndex={mode === item.id ? 0 : -1}
                  disabled={loading}
                  onClick={() => changeMode(item.id)}
                  onKeyDown={(event) => {
                    if (
                      !["ArrowLeft", "ArrowRight", "Home", "End"].includes(
                        event.key,
                      )
                    )
                      return;
                    event.preventDefault();
                    const next =
                      event.key === "Home"
                        ? "url"
                        : event.key === "End"
                          ? "file"
                          : index === 0
                            ? "file"
                            : "url";
                    changeMode(next);
                    document.getElementById(`import-tab-${next}`)?.focus();
                  }}
                >
                  <Icon name={item.icon} size={18} />
                  {item.label}
                </button>
              ))}
            </div>
            <form
              className="parse-form"
              onSubmit={submit}
              noValidate
              aria-busy={loading}
            >
              <div
                role="tabpanel"
                id="import-panel-url"
                aria-labelledby="import-tab-url"
                hidden={mode !== "url"}
              >
                <label className="sr-only" htmlFor="video-url">
                  视频链接
                </label>
                <div className="url-field">
                  <Icon name="link" size={22} />
                  <input
                    ref={inputRef}
                    id="video-url"
                    type="url"
                    value={url}
                    onChange={(event) => {
                      setUrl(event.target.value);
                      setError(null);
                    }}
                    placeholder="粘贴公开的视频链接…"
                    maxLength={8192}
                    autoComplete="url"
                    autoCapitalize="none"
                    spellCheck={false}
                    disabled={loading}
                    aria-invalid={!!error}
                    aria-describedby={error ? "home-request-error" : undefined}
                  />
                  <button
                    className="button primary"
                    disabled={loading || auth.restoring || !!auth.restoreError}
                    type="submit"
                  >
                    {loading && <span className="spinner" />}
                    {loading ? "正在解析" : "开始解析"}
                    {!loading && <Icon name="arrow" size={18} />}
                  </button>
                </div>
                <p className="home-input-note">
                  仅处理公开免登录资源，以实际解析结果为准。
                </p>
              </div>
              <div
                role="tabpanel"
                id="import-panel-file"
                aria-labelledby="import-tab-file"
                hidden={mode !== "file"}
              >
                <div
                  className={`home-file-drop${dragging ? " dragging" : ""}${file ? " has-file" : ""}`}
                  onDragOver={(event) => {
                    event.preventDefault();
                    if (!loading) setDragging(true);
                  }}
                  onDragLeave={(event) => {
                    if (
                      !event.currentTarget.contains(
                        event.relatedTarget as Node | null,
                      )
                    )
                      setDragging(false);
                  }}
                  onDrop={(event) => {
                    event.preventDefault();
                    setDragging(false);
                    if (loading) return;
                    if (event.dataTransfer.files.length !== 1)
                      setError(
                        new ApiError("UPLOAD_ONE_FILE", "请每次选择一个文件。"),
                      );
                    else choose(event.dataTransfer.files[0]);
                  }}
                >
                  <span className="home-upload-icon">
                    <Icon
                      name={
                        file
                          ? /\.(mp3|m4a)$/i.test(file.name)
                            ? "audio"
                            : "play"
                          : "upload"
                      }
                      size={29}
                    />
                  </span>
                  {file ? (
                    <>
                      <strong className="home-selected-file">
                        {file.name}
                      </strong>
                      <span className="home-file-formats">
                        {/\.(mp3|m4a)$/i.test(file.name)
                          ? /\.m4a$/i.test(file.name)
                            ? "M4A 音频"
                            : "MP3 音频"
                          : "本地视频"}{" "}
                        · {filesize(file.size)}
                        <button
                          type="button"
                          className="home-change-file"
                          disabled={loading}
                          onClick={() => fileInput.current?.click()}
                        >
                          更换文件
                        </button>
                      </span>
                      <button
                        className="button primary home-file-button"
                        type="submit"
                        disabled={
                          loading ||
                          active ||
                          auth.restoring ||
                          !!auth.restoreError
                        }
                      >
                        {loading && <span className="spinner" />}
                        {loading
                          ? "正在创建上传…"
                          : active
                            ? "请先完成当前上传"
                            : "上传并解析"}
                        {!loading && <Icon name="arrow" size={17} />}
                      </button>
                    </>
                  ) : (
                    <>
                      <strong>拖入视频或音频文件</strong>
                      <span className="home-file-formats">
                        支持 MP4、MOV、MKV、WebM、MP3、M4A
                      </span>
                      <button
                        type="button"
                        className="button primary home-file-button"
                        disabled={loading}
                        onClick={() => fileInput.current?.click()}
                      >
                        选择文件
                      </button>
                    </>
                  )}
                </div>
                <input
                  ref={fileInput}
                  type="file"
                  hidden
                  accept=".mp4,.mov,.mkv,.webm,.mp3,.m4a"
                  aria-label="选择本地视频或音频"
                  disabled={loading}
                  onChange={(event) => {
                    choose(event.target.files?.[0]);
                    event.target.value = "";
                  }}
                />
                <div className="home-file-notes">
                  <p>
                    {billing.data
                      ? `单个文件最多 ${filesize(billing.data.limits.file_bytes)}，最长 ${Math.floor(billing.data.limits.video_seconds / 60)} 分钟。`
                      : "登录后可查看当前账号的文件大小与时长限制。"}
                  </p>
                  <p>上传与基础校验不消耗转写或 AI 额度。</p>
                  {file && (
                    <p>语音识别通过云端服务处理，原文件默认保留 24 小时。</p>
                  )}
                </div>
              </div>
              <div id="import-error">
                <ErrorNotice
                  error={error}
                  id="home-request-error"
                  onDismiss={() => setError(null)}
                >
                  {mode === "url" && (
                    <>
                      {[
                        "SOURCE_ACCESS_RESTRICTED",
                        "SOURCE_RATE_LIMITED",
                        "SOURCE_NETWORK_UNAVAILABLE",
                        "SOURCE_COOLDOWN",
                        "SOURCE_EGRESS_UNAVAILABLE",
                      ].includes(error?.code || "") && (
                        <button
                          className="text-button"
                          type="button"
                          disabled={loading}
                          onClick={() => changeMode("file")}
                        >
                          改用上传文件
                        </button>
                      )}
                      <button
                        className="text-button"
                        type="button"
                        disabled={loading}
                        onClick={() => void submit()}
                      >
                        保留链接，重新尝试
                      </button>
                    </>
                  )}
                </ErrorNotice>
              </div>
            </form>
          </section>
        </div>
      </section>
      <PlatformLogos />
      {adVisible && (
        <aside className="home-ad-slot" aria-label="广告展示位">
          <span className="home-ad-label">广告</span>
          <span className="home-ad-placeholder">广告展示位</span>
          <button
            type="button"
            className="home-ad-close"
            aria-label="关闭广告"
            onClick={() => setAdVisible(false)}
          >
            <Icon name="close" size={17} />
          </button>
        </aside>
      )}
    </div>
  );
}
