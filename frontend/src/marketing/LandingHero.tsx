import Glyph from "./Glyph";
import FoldText from "./components/FoldText";

export default function LandingHero() {
  return (
    <section className="m-hero" aria-labelledby="hero-title">
      <div className="m-hero-copy">
        <h1 id="hero-title">
          <FoldText
            text={"每一帧，\n皆有所得。"}
            foldStyle="paper"
            splitBy="char"
            hinge="top"
            trigger="mount"
            foldAngle={85}
            duration={0.52}
            stagger={0.035}
            creaseShading={0.25}
            perspective={900}
            fontSize="inherit"
            fontWeight="inherit"
          />
        </h1>
        <div className="m-hero-caption">
          <p>视频切片与下载，提取文稿，生成AI总结与思维导图</p>
        </div>
      </div>

      <svg
        className="m-hero-connectors"
        viewBox="0 0 1400 880"
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <path data-hero-connector="left" d="M182 370C182 500 330 530 420 555" />
        <path
          data-hero-connector="right"
          d="M1225 470C1225 560 1090 570 980 595"
        />
      </svg>
      <div className="m-hero-side m-hero-side-left">
        <div className="m-hero-side-card">
          <span className="m-hero-side-icon" aria-hidden="true">
            <Glyph name="wave" size={20} />
          </span>
          <strong>视频与录音，多种收获。</strong>
          <p>
            从原文到理解，
            <br />
            让内容留下来。
          </p>
        </div>
      </div>
      <div className="m-hero-side m-hero-side-right">
        <div className="m-hero-side-card">
          <span className="m-hero-side-icon" aria-hidden="true">
            <Glyph name="bookmark" size={21} />
          </span>
          <strong>看过，也真正拥有。</strong>
          <p>
            文稿、总结、导图，
            <br />
            整理在同一个地方。
          </p>
        </div>
      </div>

      <div className="m-hero-stage">
        <img
          className="m-hero-composite"
          src="/media/landing/hero-composite-meditation.webp"
          alt="闭眼冥想的黑白人物周围环绕着文稿、AI 总结、思维导图和视频卡片"
          width={1593}
          height={987}
          fetchPriority="high"
          decoding="async"
        />
        <a className="m-explore-cue" href="#features">
          看看实际处理结果 <span aria-hidden="true">↓</span>
        </a>
      </div>
    </section>
  );
}
