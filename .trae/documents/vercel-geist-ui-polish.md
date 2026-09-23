# 前端 UI 打磨：Vercel / Geist 风格对齐

## Context

当前前端已有黑白极简雏形（ink/paper/line/muted token + .btn/.card 组件类），但与目标风格（参考文件 Vercel Style Reference + vercel.com 实测 branding）存在系统性差距：标题全部 font-bold(700)（规范要求 400-450）、画布纯白而非 #fafafa、文本纯黑而非 #171717、缺 mono eyebrow 标签体系、手写样式重复（分段按钮 2 处、mono meta 行 6+ 处、`!important` 尺寸覆盖 2 处）、字体声明了 Inter 却从未加载。目标：简约、高级、高端，全站对齐 Geist 体系。

约束：不改技术栈；禁渐变/玻璃拟态/drop-shadow；新增字体依赖需在提交信息说明理由；界面 zh-CN（mono 字体只用于拉丁/数字元数据，中文走系统回退）。

## 一、Design Token（tailwind.config.js）

保留 `ink/paper/line/muted/faint` 现有类名只改值（9 个 tsx 文件大量引用，改名零收益）：

- `ink: #171717`（原 #000，正文/按钮随之切换）；新增 `brand: #000000`（仅 ▼ glyph）
- `faint: #fafafa` 作为 body 画布；`paper` 保持 #fff 作卡片面
- 新增 `ok: #297a3a`（唯一彩色：✓ 成功标记）；`graphite: #8f8f8f`（footer 次级）
- `fontFamily.sans`: `"Geist Sans", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif`；`mono`: `"Geist Mono", "SF Mono", SFMono-Regular, Consolas, monospace`
- `fontSize` 新增：`eyebrow: ["11px", {lineHeight:"16px", letterSpacing:"0.071em"}]`、`caption: ["13px","20px"]`、`heading: ["30px",{lineHeight:"38px",letterSpacing:"-0.02em"}]`、`heading-lg: ["56px",{lineHeight:"1",letterSpacing:"-0.06em"}]`
- `boxShadow.hairline: "0 0 0 1px rgba(0,0,0,0.08), 0 0 0 2px #fafafa"`（双环卡片边，备用）
- 圆角不加 token：`rounded-md` 本就是 6px；`.card` 的 `rounded-lg`(8px) 改 `rounded-md`

## 二、字体加载

- 装 `@fontsource/geist-sans` + `@fontsource/geist-mono`（5.3.0，自托管，不依赖 CDN；variable 版 geist-sans 在 npm 不存在，450 字重放弃，标题统一 400——规范允许 400-450）
- `main.tsx` 顶部 import `geist-sans/400.css`、`geist-sans/500.css`、`geist-mono/400.css`、`geist-mono/500.css`（fontsource 自带 font-display: swap）

## 三、index.css 组件类扩展

- `body` 改 `@apply bg-faint text-ink`
- 新增：`.eyebrow`（mono 11px uppercase 0.071em muted）、`.meta`（mono 13px muted，仅拉丁/数字）、`.page-title`（30px/400/tracking-tight）、`.btn-lg`（h-12 px-6 text-base）、`.btn-sm`（h-8 px-3）、`.field-lg`（h-12 px-4 text-base）、`.seg-item`/`.seg-item-active`/`.seg-item-idle`（分段选择）、`.section-divider`（border-t pt-4）、`.list-row`（Admin 列表行）

## 四、逐文件改造点

- **App.tsx**：主容器 `max-w-5xl` → `max-w-7xl`（1280px）
- **Nav.tsx**：黑方块 logo → `▼` glyph（text-brand，呼应下载语义）；配额行 → `.meta`；注册按钮去 `!h-8 !px-3` → `.btn-sm`；Footer 同步容器宽度
- **Home.tsx**：L86 → `.eyebrow`；hero 标题 `font-bold` → `text-heading-lg`-ish 级（font-normal，负字距）；副文 text-muted
- **VideoCard.tsx**：格式选择 → `.seg-item*`；UrlForm 去 `!important` → `.field-lg`/`.btn-lg`；meta 行 → `.meta`
- **AiPanel.tsx**：`text-[11px]` → `text-eyebrow`；3 处 `border-t pt-4` → `.section-divider`
- **Auth.tsx**：标题 → `.page-title`，补 `.eyebrow`（LOGIN / REGISTER）
- **History.tsx**：标题 → `.page-title` + eyebrow；meta → `.meta`
- **Plans.tsx**：`tracking-[0.2em]` → `.eyebrow`；标题/价格 font-bold → 400；渠道切换 → `.seg-item*`；已含权益 ✓ 与 PayPage 已支付 ✓ 加 `text-ok`
- **Admin.tsx**：标题/eyebrow 归一；4 处列表行 → `.list-row`，**中文列去掉 font-mono**（仅订单号/时间戳保留 mono）

## 五、Commit 划分（5 个，各自可独立回滚）

1. tokens：tailwind.config.js 重映射 + 新增 token
2. 字体：装 2 个 fontsource 依赖 + main.tsx 引入（message 注明新增依赖理由）
3. 组件类：index.css 扩展 + body 画布切换
4. 去重：VideoCard/Plans/AiPanel/Admin/Nav 换用新组件类
5. 排版归一：标题字重、eyebrow 补齐、1280 容器、▼ logo、text-ok 点缀

## 六、验证

- 每个 commit 前：`npm run test`（utils.test.ts 全绿）、`npm run lint`、`npm run build`（含 tsc 类型检查）
- 视觉走查清单：画布 #fafafa 与白卡片可区分；全站无 drop-shadow/渐变；eyebrow 0.071em；hero 中文标题 400 不发虚；mono 不出现在中文正文；卡片/按钮/输入框统一 6px；无 `!important`；✓ 为 #297a3a；1280 下 Nav 与 main 左右对齐
- 最后用 `docker compose` 或 vite dev 实际打开首页/套餐/管理页人工核对
