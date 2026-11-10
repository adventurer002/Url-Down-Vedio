import { useRef, useState } from "react";
import { cleanPath } from "./routes";
import { example, faqs } from "./content";
import Glyph, { type GlyphName } from "./Glyph";
import { useLandingMotion } from "./useLandingMotion";
import LandingHero from "./LandingHero";
import ProductShowcase from "./ProductShowcase";

const landingNavItems = [
  { label: "功能介绍", href: "#features" },
  { label: "使用流程", href: "#how-it-works" },
  { label: "常见问题", href: "#faq" },
];

function Brand() {
  return (
    <a className="m-brand" href="/" aria-label="VideoHub AI 首页">
      <span>
        VideoHub<span className="m-brand-ai">AI</span>
      </span>
    </a>
  );
}
function Action({
  children = "开始体验",
  href = "/app",
  light = false,
}: {
  children?: React.ReactNode;
  href?: string;
  light?: boolean;
}) {
  return (
    <a className={`m-action${light ? " m-action-light" : ""}`} href={href}>
      {children}
      <Glyph name="arrow" size={17} />
    </a>
  );
}
function Header({
  landing = false,
  activeSection = "",
  onSelectSection,
}: {
  landing?: boolean;
  activeSection?: string;
  onSelectSection?: (section: string) => void;
}) {
  if (landing) {
    return (
      <header className="m-header-reference">
        <div className="m-reference-bar">
          <Brand />
          <nav className="m-reference-nav" aria-label="主导航">
            <a
              className="m-reference-play"
              href="/app"
              aria-label="打开视频工作台"
            >
              <Glyph name="play" size={18} />
            </a>
            {landingNavItems.map((item) => (
              <a
                key={item.href}
                href={item.href}
                aria-current={
                  item.href === `#${activeSection}` ? "location" : undefined
                }
                onClick={() => onSelectSection?.(item.href.slice(1))}
              >
                {item.label}
              </a>
            ))}
          </nav>
          <a className="m-reference-app" href="/app">
            进入工作台 <span aria-hidden="true">↗</span>
          </a>
        </div>
      </header>
    );
  }
  return (
    <header className="m-header">
      <nav className="m-nav" aria-label="主导航">
        <Brand />
        <div className="m-nav-links">
          <a href="/#features">功能介绍</a>
          <a href="/examples">处理示例</a>
        </div>
        <a className="m-nav-app" href="/app">
          进入工作台
          <Glyph name="arrow" size={14} />
        </a>
        <details className="m-mobile-nav">
          <summary aria-label="展开导航菜单">菜单</summary>
          <div>
            <a href="/#features">功能介绍</a>
            <a href="/examples">处理示例</a>
            <a href="/help">使用帮助</a>
          </div>
        </details>
      </nav>
    </header>
  );
}
function Footer() {
  return (
    <footer className="m-footer">
      <div>
        <Brand />
        <p>让每一次观看，都有所收获。</p>
      </div>
      <nav aria-label="页尾导航">
        <a href="/examples">处理示例</a>
        <a href="/help">使用帮助</a>
        <a href="/subscription">订阅方案</a>
        <a href="/privacy">隐私说明</a>
      </nav>
      <small>VIDEOHUB AI · 公开内容整理工具</small>
    </footer>
  );
}
function FAQ({ all = false }: { all?: boolean }) {
  return (
    <div className="m-faq-list">
      {(all ? faqs : [faqs[2], faqs[4], faqs[5], faqs[7], faqs[6]]).map(
        (item) => (
          <details key={item.question}>
            <summary>
              {item.question}
              <span aria-hidden="true">+</span>
            </summary>
            <p>{item.answer}</p>
          </details>
        ),
      )}
    </div>
  );
}
function Landing() {
  return (
    <>
      <LandingHero />
      <ProductShowcase />
      <section
        className="m-section m-process"
        id="how-it-works"
        data-nav-section="how-it-works"
      >
        <div className="m-section-heading" data-reveal="heading">
          <p className="m-eyebrow">简单开始</p>
          <h2>从一个链接开始。</h2>
          <p>粘贴链接、选择处理，再回到原文核对。</p>
        </div>
        <ol data-reveal="steps">
          {[
            {
              title: "粘贴视频链接",
              text: "在工作台粘贴公开视频链接，通过微信登录后解析。",
            },
            {
              title: "选择你需要的内容",
              text: "选择下载、文稿、总结或导图，确认额度后开始处理。",
            },
            {
              title: "查看、核对，再收藏",
              text: "按时间点回看原文，将有用的文稿和总结加入知识库。",
            },
          ].map((step, index) => (
            <li key={step.title}>
              <span className="m-step-number">0{index + 1}</span>
              <h3>{step.title}</h3>
              <p>{step.text}</p>
            </li>
          ))}
        </ol>
      </section>
      <section
        className="m-section m-use-cases"
        id="about"
        data-nav-section="how-it-works"
      >
        <div className="m-section-heading" data-reveal="heading">
          <h2>用在你的学习与工作中。</h2>
        </div>
        <div data-reveal="content">
          {[
            {
              name: "学习课程",
              text: "将课程整理成章节和笔记，复习时按问题找到内容。",
              tag: "理解 → 复习",
            },
            {
              name: "整理访谈",
              text: "沿着文稿回顾观点，核对上下文，减少来回寻找的时间。",
              tag: "检索 → 核对",
            },
            {
              name: "研究主题",
              text: "围绕一个问题收藏相关视频，用总结和导图建立线索。",
              tag: "连接 → 积累",
            },
          ].map((item) => (
            <article key={item.name}>
              <span>{item.tag}</span>
              <h3>{item.name}</h3>
              <p>{item.text}</p>
            </article>
          ))}
        </div>
      </section>
      <section className="m-section m-faq" id="faq" data-nav-section="faq">
        <div data-reveal="heading">
          <h2>开始前，你可能想了解。</h2>
          <a className="m-inline-link" href="/help">
            查看完整使用帮助 <Glyph name="arrow" size={17} />
          </a>
        </div>
        <div data-reveal="content">
          <FAQ />
        </div>
      </section>
      <section className="m-closing">
        <span className="m-closing-play" aria-hidden="true">
          <Glyph name="play" size={25} />
        </span>
        <h2 data-reveal="heading">
          让下一次观看，
          <br />
          成为一次新的积累。
        </h2>
        <Action />
        <p>从一段你真正感兴趣的视频开始。</p>
      </section>
    </>
  );
}
function PageIntro({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <header className="m-page-intro">
      <p className="m-eyebrow">{eyebrow}</p>
      <h1>{title}</h1>
      <p>{children}</p>
    </header>
  );
}
function Examples() {
  return (
    <div className="m-document">
      <nav className="m-breadcrumb" aria-label="面包屑">
        <a href="/">首页</a>
        <span>/</span>
        <span>处理示例</span>
      </nav>
      <PageIntro
        eyebrow="SEE THE SOURCE. FOLLOW THE RESULT."
        title="一段视频，几种新的可能。"
      >
        从一份可核对的来源，看看文稿、总结与思维导图如何对应起来。
      </PageIntro>
      <section className="m-example-index">
        <div className="m-example-poster">
          <Glyph name="play" size={40} />
          <span>00:48 · 自制字幕短片</span>
        </div>
        <div>
          <p className="m-eyebrow">公开处理示例 01</p>
          <h2>{example.title}</h2>
          <p>{example.description}</p>
          <p className="m-subtle">
            本地演示模型处理，提供原片、字幕与完整结果。
          </p>
          <a className="m-inline-link" href="/#features">
            查看处理演示 <Glyph name="arrow" />
          </a>
        </div>
      </section>
      <div className="m-document-note">
        公开示例由站点单独制作，不来自用户的私人任务或收藏。
      </div>
    </div>
  );
}
function Help() {
  return (
    <div className="m-document">
      <PageIntro eyebrow="A LITTLE GUIDANCE" title="从链接，到你的知识。">
        了解如何解析视频、查看处理结果，以及每一步的使用规则。
      </PageIntro>
      <section className="m-help-steps">
        <h2>第一次使用</h2>
        <ol>
          <li>
            <strong>准备链接。</strong>进入<a href="/app">视频工作台</a>
            ，粘贴公开、免登录的视频链接。
          </li>
          <li>
            <strong>登录并解析。</strong>
            关注公众号，按登录窗口提示发送口令。登录后解析视频，查看实际可用的处理内容与档位。
          </li>
          <li>
            <strong>选择并确认。</strong>
            选择下载、文稿、总结或导图，阅读额度确认后开始。关闭页面不会取消已创建的任务。
          </li>
          <li>
            <strong>查看并保存。</strong>
            从任务历史查看进度。结果完成后可搜索文稿、复制或导出，并将文稿和总结收藏到知识库。
          </li>
        </ol>
      </section>
      <section className="m-help-faq">
        <h2>常见问题与产品范围</h2>
        <FAQ all />
      </section>
      <section className="m-document-note">
        <h2>重要内容请回到来源核对</h2>
        <p>
          平台字幕与语音转写都可能存在识别或时间误差。AI
          总结和导图用于辅助理解，不替代原视频、原文或专业判断。
        </p>
        <a className="m-inline-link" href="/#features">
          查看首页处理演示 <Glyph name="arrow" size={17} />
        </a>
      </section>
      <div className="m-document-end">
        <Action />
      </div>
    </div>
  );
}
function Privacy() {
  return (
    <div className="m-document">
      <PageIntro
        eyebrow="YOUR CONTENT, WITH CONTEXT"
        title="隐私与内容处理说明"
      >
        了解使用 VideoHub AI
        时，哪些信息会被处理，以及公开示例与私人内容的边界。
      </PageIntro>
      <div className="m-prose">
        {[
          [
            "账户与任务记录",
            "通过微信公众号登录时，服务会处理微信用户标识、昵称、关注状态及登录时间，以识别账号和提供相应权益。提交的视频链接、任务状态和处理结果保存在当前服务中，并按账号权限访问。",
          ],
          [
            "浏览器中的信息",
            "浏览器会保存登录 Cookie、登录凭证及当前账号的工作台记录，用于恢复登录与任务进度。退出登录会清除浏览器当前登录状态。已经下载到本地的文件由你保管。",
          ],
          [
            "第三方内容与模型服务",
            "视频解析会访问对应视频平台。启用云端语音识别或语言模型时，处理所需的音频、文稿或问题会发送给当前服务配置的提供方。具体提供方及保留规则以当前部署配置为准。",
          ],
          [
            "文件与结果保留",
            "下载文件按服务配置到期清理。文稿、总结、任务记录和收藏的可用性受服务保留与访问规则约束。请及时导出需要自行保管的内容。",
          ],
          [
            "公开示例与搜索",
            "站点公开示例使用单独制作并说明来源的素材。私人任务、个人中心、知识库及管理页面不作为公开搜索内容。搜索展示控制不替代账号鉴权。",
          ],
          [
            "使用范围",
            "本服务面向可公开访问的免登录资源。请使用你有权处理的内容，并在发布或引用结果时核对原始来源。",
          ],
        ].map(([title, text]) => (
          <section key={title}>
            <h2>{title}</h2>
            <p>{text}</p>
          </section>
        ))}
      </div>
      <a className="m-inline-link" href="/help">
        继续阅读使用帮助 <Glyph name="arrow" />
      </a>
    </div>
  );
}
function Missing() {
  return (
    <div className="m-missing">
      <p className="m-eyebrow">404 / PAGE NOT FOUND</p>
      <h1>这一页，暂时找不到。</h1>
      <p>链接可能有误，你可以返回首页，或直接进入工作台。</p>
      <div>
        <Action href="/">返回首页</Action>
        <a className="m-text-link" href="/app">
          进入工作台 ↗
        </a>
      </div>
    </div>
  );
}
export default function MarketingSite({ path }: { path: string }) {
  const pathname = cleanPath(path);
  const rootRef = useRef<HTMLDivElement>(null);
  const [activeSection, setActiveSection] = useState("");
  useLandingMotion(rootRef, pathname === "/", setActiveSection);
  return (
    <div
      ref={rootRef}
      className={`marketing-site${pathname === "/" ? " m-landing-site" : ""}`}
    >
      {pathname === "/" && (
        <div className="m-reading-progress" aria-hidden="true">
          <span />
        </div>
      )}
      <a className="m-skip" href="#marketing-main">
        跳转至主要内容
      </a>
      <Header
        landing={pathname === "/"}
        activeSection={activeSection}
        onSelectSection={setActiveSection}
      />
      <main id="marketing-main">
        {pathname === "/" ? (
          <Landing />
        ) : pathname === "/help" ? (
          <Help />
        ) : pathname === "/privacy" ? (
          <Privacy />
        ) : pathname === "/examples" ? (
          <Examples />
        ) : (
          <Missing />
        )}
      </main>
      <Footer />
    </div>
  );
}
