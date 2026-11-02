import example from "./generated/example.json";
export { example };

export const siteOrigin = (
  import.meta.env.VITE_SITE_URL || "https://vedio.vip"
).replace(/\/$/, "");
export const productDescription =
  "VideoHub AI 是一款在线视频内容整理工具，可提取字幕或进行语音转写，生成章节总结与思维导图，并支持视频下载和知识收藏。";

export const faqs = [
  {
    question: "VideoHub AI 可以帮我做什么？",
    answer: productDescription + "适合课程学习、访谈整理和公开视频研究。",
  },
  {
    question: "有字幕与没有字幕的视频，处理方式有什么不同？",
    answer:
      "系统优先使用可用的平台字幕；需要语音转写时，通过配置的语音识别服务生成文稿。已有字幕及转写缓存不扣转写分钟，实际处理以任务前的额度确认和服务规则为准。",
  },
  {
    question: "支持哪些视频链接？",
    answer:
      "支持解析器能够访问的公开、免登录视频。平台策略、网络条件和单个视频状态会影响可用性，请在工作台粘贴链接，以实际解析结果为准。当前不提供用户 Cookie 上传或自定义代理。",
  },
  {
    question: "文稿、总结和思维导图有什么区别？",
    answer:
      "文稿用于检索具体内容；总结提炼主要观点与章节；思维导图把主题关系展开。总结和导图是整理结果，重要信息应回到原文和原视频核对。",
  },
  {
    question: "如何开始，需要登录吗？",
    answer:
      "进入工作台后粘贴视频链接。首次处理前，通过微信公众号口令登录，再选择处理内容并确认额度。公开的产品说明和处理示例无需登录即可阅读。",
  },
  {
    question: "怎样确认本次使用的额度？",
    answer:
      "转写分钟、AI 次数和下载权益分别计算。处理前会显示本次最高占用，最终以实际执行及结算规则为准。失败或有效取消释放占用；可在个人中心查看余额、占用和到期信息。",
  },
  {
    question: "现在可以在线购买会员吗？",
    answer:
      "目前采用人工权益发放模式，尚未开放在线购买。订阅页面展示当前套餐及使用规则，已经获得的权益可在个人中心查看。",
  },
  {
    question: "文件和处理结果能保留多久？",
    answer:
      "下载文件有保留期限，到期后可能被清理，请及时保存。具体期限以当前服务配置及任务提示为准。收藏用于组织已有文稿和总结，不等同于永久保存下载文件。",
  },
];

export const features = [
  {
    icon: "play",
    number: "01",
    title: "下载视频，留住原片",
    text: "按解析结果选择可用的视频或音频档位，任务完成后保存到本地。",
    detail: "可用档位以原视频与解析结果为准。",
  },
  {
    icon: "file",
    number: "02",
    title: "提取文稿，让内容可检索",
    text: "从平台字幕或语音转写获得文稿，搜索关键词，按时间位置找回上下文。",
    detail: "文稿支持复制及 TXT / SRT 导出。",
  },
  {
    icon: "summary",
    number: "03",
    title: "读懂主线，抓住关键观点",
    text: "将较长的内容整理成概览、要点和章节，先建立理解，再回到原文核对。",
    detail: "生成内容应结合原文判断。",
  },
  {
    icon: "nodes",
    number: "04",
    title: "展开导图，看清知识结构",
    text: "用分支串联主题与章节，让零散观点成为可回顾、可继续整理的结构。",
    detail: "与总结一起查看，按自己的问题继续探索。",
  },
] as const;

export function metadata(path: string) {
  const pages: Record<string, { title: string; description: string }> = {
    "/": {
      title: "VideoHub AI｜AI 视频总结、视频转文字与思维导图",
      description:
        "使用 VideoHub AI 整理公开视频内容，提取字幕或语音转写，生成 AI 总结与思维导图，并支持视频下载和知识收藏。查看处理示例、使用流程与额度规则。",
    },
    "/examples": {
      title: "视频处理示例｜文稿、总结与思维导图 — VideoHub AI",
      description:
        "查看 VideoHub AI 的自制公开视频示例，核对原始字幕、本地演示总结器的处理结果、章节与思维导图。",
    },
    "/examples/knowledge-loop": {
      title: "让看过的内容真正留下来｜公开处理示例 — VideoHub AI",
      description:
        "一段 48 秒自制字幕短片的公开处理记录，包含原视频、字幕、章节总结、思维导图与处理来源说明，可逐项核对。",
    },
    "/help": {
      title: "使用帮助｜视频转写、AI 总结与额度规则 — VideoHub AI",
      description:
        "了解 VideoHub AI 的视频解析、字幕与语音转写、AI 总结、思维导图、微信公众号登录、额度确认和文件保留规则。",
    },
    "/privacy": {
      title: "隐私说明｜账户与内容处理 — VideoHub AI",
      description:
        "了解 VideoHub AI 如何处理账户标识、视频链接、文稿、模型服务及浏览器中的登录信息，公开示例与用户私人内容分开管理。",
    },
    "/subscription": {
      title: "订阅方案与额度规则 — VideoHub AI",
      description:
        "查看 VideoHub AI 免费体验、月度会员与加购包的套餐说明，了解转写、AI 次数、下载和有效期规则。目前尚未开放在线购买。",
    },
  };
  return (
    pages[path] ?? {
      title: "页面未找到 — VideoHub AI",
      description: "此页面不存在，请返回 VideoHub AI 首页或进入视频工作台。",
    }
  );
}
