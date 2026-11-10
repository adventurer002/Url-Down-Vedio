import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import MarketingSite from "./MarketingSite";
import { metadata, siteOrigin, productDescription } from "./content";
import { cleanPath, documentPaths } from "./routes";
import SubscriptionView from "../views/SubscriptionView";
import AppHeader from "../components/AppHeader";
import AppFooter from "../components/AppFooter";
import { WorkspaceProvider } from "../hooks/useWorkspace";

const escape = (value: string) =>
  value
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
const json = (value: unknown) => JSON.stringify(value).replace(/</g, "\\u003c");

export function publicDocument(
  template: string,
  rawPath: string,
  options: { styles: string[]; indexing?: boolean; empty?: boolean },
) {
  const path = cleanPath(rawPath);
  const known = documentPaths.some((item) => item === path);
  const publicPage = known && !options.empty;
  const info =
    publicPage || !options.empty
      ? metadata(path)
      : {
          title: "VideoHub AI · 视频工作台",
          description: "登录后解析视频，查看你的任务、文稿与 AI 处理结果。",
        };
  const origin = new URL(siteOrigin);
  if (
    origin.protocol !== "https:" ||
    origin.username ||
    origin.password ||
    origin.pathname !== "/" ||
    origin.search ||
    origin.hash
  )
    throw new Error(
      "VITE_SITE_URL must be an HTTPS origin without credentials or a path",
    );
  const url = `${origin.origin}${path}`;
  const graph: Record<string, unknown>[] = [
    {
      "@type": "WebSite",
      "@id": `${origin.origin}/#website`,
      name: "VideoHub AI",
      url: `${origin.origin}/`,
      description: productDescription,
      inLanguage: "zh-CN",
    },
    {
      "@type": "WebPage",
      "@id": `${url}#page`,
      url,
      name: info.title,
      description: info.description,
      inLanguage: "zh-CN",
      isPartOf: { "@id": `${origin.origin}/#website` },
    },
  ];
  if (path === "/")
    graph.push({
      "@type": "SoftwareApplication",
      "@id": `${origin.origin}/#application`,
      name: "VideoHub AI",
      url: `${origin.origin}/`,
      applicationCategory: "MultimediaApplication",
      operatingSystem: "Web",
      description: productDescription,
      featureList: [
        "视频下载",
        "字幕与语音转写",
        "AI 总结",
        "思维导图",
        "知识收藏",
      ],
    });
  if (path !== "/")
    graph.push({
      "@type": "BreadcrumbList",
      itemListElement: [
        {
          "@type": "ListItem",
          position: 1,
          name: "首页",
          item: `${origin.origin}/`,
        },
        {
          "@type": "ListItem",
          position: 2,
          name: info.title.split("｜")[0].split(" — ")[0],
          item: url,
        },
      ],
    });
  const head = [
    `<meta name="description" content="${escape(info.description)}">`,
    `<meta name="robots" content="${publicPage && options.indexing !== false ? "index,follow" : "noindex,follow"}">`,
    ...options.styles.map(
      (style) => `<link rel="stylesheet" href="${escape(style)}">`,
    ),
    ...(publicPage
      ? [
          `<link rel="canonical" href="${escape(url)}">`,
          `<meta property="og:type" content="website"><meta property="og:site_name" content="VideoHub AI"><meta property="og:locale" content="zh_CN">`,
          `<meta property="og:title" content="${escape(info.title)}"><meta property="og:description" content="${escape(info.description)}"><meta property="og:url" content="${escape(url)}">`,
          `<meta property="og:image" content="${origin.origin}/media/landing/social-cover.png"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630"><meta property="og:image:alt" content="VideoHub AI：把视频变成你的知识，提供视频下载、文稿、总结与导图。">`,
          `<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="${escape(info.title)}"><meta name="twitter:description" content="${escape(info.description)}"><meta name="twitter:image" content="${origin.origin}/media/landing/social-cover.png">`,
          `<script type="application/ld+json">${json({ "@context": "https://schema.org", "@graph": graph })}</script>`,
        ]
      : []),
  ].join("\n");
  let content = "";
  if (!options.empty)
    content =
      path === "/subscription"
        ? renderToString(
            <MemoryRouter initialEntries={["/subscription"]}>
              <WorkspaceProvider>
                <div className="public-app">
                  <AppHeader />
                  <main id="main" tabIndex={-1}>
                    <SubscriptionView />
                  </main>
                  <AppFooter />
                </div>
              </WorkspaceProvider>
            </MemoryRouter>,
          )
        : renderToString(<MarketingSite path={path} />);
  return template
    .replace(/<title>[\s\S]*?<\/title>/, `<title>${escape(info.title)}</title>`)
    .replace(/<meta\s+name="description"[^>]*>/, "")
    .replace("</head>", `${head}\n</head>`)
    .replace('<div id="app"></div>', `<div id="app">${content}</div>`);
}
