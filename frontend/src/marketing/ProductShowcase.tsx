import { useState } from "react";
import { example, features } from "./content";
import Glyph from "./Glyph";

const labels = ["视频下载", "文稿提取", "章节总结", "思维导图"];
const exampleMediaPath = "/media/examples/knowledge-loop";

/** Display an excerpt of the published output, without changing that output. */
function firstSentence(title: string) {
  const content = title.replace(/^章节 \d+：/, "");
  return content.match(/^[^。！？]+[。！？]?/)?.[0] || content;
}

function ExampleMindmap({
  expanded,
  onExpand,
}: {
  expanded: number | null;
  onExpand: (index: number | null) => void;
}) {
  return (
    <div className="m-showcase-map">
      <span className="m-showcase-map-root">让知识留下来</span>
      <div className="m-showcase-branches">
        {example.mindmap.children.map((branch, index) => (
          <details key={branch.title} open={expanded === index}>
            <summary
              onClick={(event) => {
                event.preventDefault();
                onExpand(expanded === index ? null : index);
              }}
            >
              <span>
                {branch.title.replace(/^章节 \d+：/, "").split(/[，。]/)[0]}
              </span>
              <span className="m-node-toggle" aria-hidden="true">
                +
              </span>
            </summary>
            <div>
              {branch.children.map((child) => (
                <p key={child.title}>{child.title}</p>
              ))}
            </div>
          </details>
        ))}
      </div>
    </div>
  );
}

export default function ProductShowcase() {
  const [selected, setSelected] = useState(2);
  const [expandedBranch, setExpandedBranch] = useState<number | null>(null);
  const select = (index: number) => {
    setSelected(index);
    // Hidden map details must not hold the other previews at their expanded height.
    if (index !== 3) setExpandedBranch(null);
  };
  return (
    <section
      className="m-section m-showcase"
      id="features"
      data-nav-section="features"
      aria-labelledby="showcase-title"
    >
      <span id="example" className="m-anchor-alias" aria-hidden="true" />
      <div className="m-showcase-heading" data-reveal="heading">
        <div>
          <h2 id="showcase-title">
            <span>一段视频，</span>
            <span>四种收获。</span>
          </h2>
          <p>下载原片、检索文稿，或沿着章节和导图回顾重点。</p>
        </div>
        <a
          className="m-inline-link"
          href={`${exampleMediaPath}/result.json`}
          download
        >
          下载处理记录 <Glyph name="arrow" size={18} />
        </a>
      </div>
      <div className="m-showcase-window" data-reveal="panel">
        <div className="m-showcase-source">
          <a
            href={`${exampleMediaPath}/source.mp4`}
            aria-label="查看示例原片"
            className="m-showcase-poster"
          >
            <img
              src="/media/examples/knowledge-loop/poster.webp"
              width={960}
              height={540}
              loading="lazy"
              alt="自制示例短片封面"
            />
            <Glyph name="play" size={18} />
          </a>
          <div>
            <h3>{example.title}</h3>
            <p>48 秒自制字幕短片 · 本地演示模型处理</p>
          </div>
        </div>
        <div
          className="m-showcase-tabs"
          role="tablist"
          aria-label="探索产品能力"
        >
          {features.map((feature, index) => (
            <button
              type="button"
              key={feature.icon}
              id={`feature-${feature.icon}`}
              role="tab"
              aria-controls={`feature-panel-${feature.icon}`}
              aria-selected={selected === index}
              tabIndex={selected === index ? 0 : -1}
              onClick={() => select(index)}
              onKeyDown={(event) => {
                const next =
                  event.key === "ArrowRight" || event.key === "ArrowDown"
                    ? (index + 1) % features.length
                    : event.key === "ArrowLeft" || event.key === "ArrowUp"
                      ? (index + features.length - 1) % features.length
                      : event.key === "Home"
                        ? 0
                        : event.key === "End"
                          ? features.length - 1
                          : -1;
                if (next < 0) return;
                event.preventDefault();
                select(next);
                document
                  .getElementById(`feature-${features[next].icon}`)
                  ?.focus();
              }}
            >
              <Glyph name={feature.icon} size={19} />
              <span>{labels[index]}</span>
            </button>
          ))}
        </div>
        <div className="m-showcase-panels">
          {features.map((feature, index) => (
            <section
              key={feature.icon}
              id={`feature-panel-${feature.icon}`}
              className="m-showcase-panel"
              role="tabpanel"
              aria-labelledby={`feature-${feature.icon}`}
              aria-hidden={selected !== index}
              inert={selected !== index}
              tabIndex={selected === index ? 0 : -1}
            >
              <div className="m-showcase-description">
                <h3>{feature.title}</h3>
                <p>{feature.text}</p>
                <small>{feature.detail}</small>
              </div>
              <div className="m-showcase-output">
                {index === 0 ? (
                  <div className="m-showcase-download">
                    <img
                      src="/media/examples/knowledge-loop/poster.webp"
                      width={960}
                      height={540}
                      alt="让看过的内容真正留下来：示例原片封面"
                      loading="lazy"
                    />
                    <a
                      href="/media/examples/knowledge-loop/source.mp4"
                      download
                    >
                      <Glyph name="file" size={20} />
                      <span>
                        下载示例原片<small>MP4 · 00:48</small>
                      </span>
                      <Glyph name="arrow" size={18} />
                    </a>
                  </div>
                ) : index === 1 ? (
                  <div className="m-showcase-transcript">
                    {example.segments.slice(0, 3).map((segment) => (
                      <p key={segment.start}>
                        <a
                          href={`${exampleMediaPath}/source.mp4#t=${segment.start}`}
                          aria-label={`回看 ${segment.start} 秒处原片`}
                        >
                          00:{String(segment.start).padStart(2, "0")}
                        </a>
                        <span>{segment.text}</span>
                      </p>
                    ))}
                  </div>
                ) : index === 2 ? (
                  <ol className="m-showcase-chapters">
                    {example.summary.chapters.map((chapter) => (
                      <li key={chapter.start_seconds}>
                        <a
                          href={`${exampleMediaPath}/source.mp4#t=${chapter.start_seconds}`}
                          aria-label={`回看 ${chapter.start_seconds} 秒处章节原片`}
                        >
                          <time>{chapter.timestamp.slice(3)}</time>
                        </a>
                        <p>{firstSentence(chapter.title)}</p>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <ExampleMindmap
                    expanded={expandedBranch}
                    onExpand={setExpandedBranch}
                  />
                )}
              </div>
            </section>
          ))}
        </div>
        <div className="m-showcase-footer">
          <span>示例仅展示处理方式，实际结果以任务为准。</span>
          <a className="m-inline-link" href="/app">
            开始体验 <Glyph name="arrow" size={16} />
          </a>
        </div>
      </div>
    </section>
  );
}
