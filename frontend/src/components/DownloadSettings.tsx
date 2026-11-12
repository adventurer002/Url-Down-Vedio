import { useState } from "react";
import type { DownloadOption, JobKind } from "../types/api";
import { filesize } from "../utils";
import Icon from "./Icon";

export default function DownloadSettings({
  options,
  downloading,
  extracting,
  onCreate,
}: {
  options: DownloadOption[];
  downloading: boolean;
  extracting: boolean;
  onCreate: (kind: JobKind, label: string, presetId?: string) => void;
}) {
  const [format, setFormat] = useState("");
  const [audio, setAudio] = useState(false);
  const [selectedId, setSelectedId] = useState("");
  const videos = options.filter((option) => option.height != null);
  const containers = [...new Set(videos.map((option) => option.container))];
  const currentContainer = containers.includes(format)
    ? format
    : containers[0] || "";
  const formats = videos.filter(
    (option) => option.container === currentContainer,
  );
  const selected =
    formats.find((option) => option.id === selectedId) || formats[0];
  const pending = audio ? extracting : downloading;

  return (
    <div className="download-settings">
      <div className="download-settings-heading">
        <h3>下载设置</h3>
        <div
          className="download-media-toggle"
          role="group"
          aria-label="下载内容"
        >
          <button
            type="button"
            aria-pressed={!audio}
            onClick={() => setAudio(false)}
          >
            视频
          </button>
          <button
            type="button"
            aria-pressed={audio}
            onClick={() => setAudio(true)}
          >
            仅音频
          </button>
        </div>
      </div>
      {!audio && (
        <fieldset className="quality-options" disabled={downloading}>
          <legend>清晰度</legend>
          {formats.map((option) => (
            <label
              key={option.id}
              className={
                "quality-option" +
                (selected?.id === option.id ? " selected" : "")
              }
            >
              <input
                type="radio"
                name="video-quality"
                value={option.id}
                checked={selected?.id === option.id}
                onChange={() => setSelectedId(option.id)}
              />
              <span className="quality-label">{option.label}</span>
              <span className="quality-size">
                {filesize(option.estimated_size_bytes)}
              </span>
            </label>
          ))}
          {!formats.length && <p className="muted">该视频暂无可下载的档位。</p>}
        </fieldset>
      )}
      <div className="container-setting">
        <label htmlFor="download-container">格式</label>
        <select
          id="download-container"
          value={audio ? "mp3" : currentContainer}
          disabled={pending || audio}
          onChange={(event) => setFormat(event.target.value)}
        >
          {audio ? (
            <option value="mp3">MP3</option>
          ) : containers.length ? (
            containers.map((container) => (
              <option key={container} value={container}>
                {container.toUpperCase()} 视频
              </option>
            ))
          ) : (
            <option value="" disabled>
              暂无视频格式
            </option>
          )}
        </select>
        <span className="muted">
          {audio ? "仅包含音频。" : "包含视频与音频。"}
        </span>
      </div>
      <button
        className="button primary full download-submit"
        disabled={pending || (!audio && !selected)}
        onClick={() => {
          if (audio) onCreate("audio_extraction", "纯音频 MP3");
          else if (selected) onCreate("download", selected.label, selected.id);
        }}
      >
        {pending ? (
          <span className="spinner" />
        ) : (
          <Icon name="download" size={22} />
        )}
        {pending ? "正在处理…" : audio ? "提取音频" : "下载视频"}
      </button>
      <p className="download-rights">下载内容仅供合法授权范围内使用。</p>
    </div>
  );
}
