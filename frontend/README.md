# VideoHub AI 前端 · React 迁移版

精彩切片 F1、F1.1 和 F2 已完成：分析报价、候选审阅、视频生成、逐条下载与历史恢复，见 [F2 交付说明](CLIPS_F2.md)。发布模板的创建入口默认关闭；本地开发可在 `frontend/.env.local` 配置 `VITE_CLIPS_ENABLED=true`，或使用 `VITE_CLIPS_ENABLED=true pnpm dev`。该分区位于导入内容后的内容工作台，不是首页顶级导航。服务端仍需启用 `CLIPS_ENABLED`，视频生成还需配置定位模型。

切片时长现默认由 AI 按语义完整性判断，固定秒数为可选模式；兼容与验收见 [自动时长交付说明](../backend/CLIPS_AUTO_DURATION.md)。

最新的落地页审查前四项修复与验收，见 [慢加载、故障保留、对比度与横屏适配](LANDING_AUDIT_FIXES.md)。整页布局、内容与交互优化记录见 [落地页整体优化与验收](LANDING_REFINEMENT.md)。

落地页 GooeyNav 导航与 FoldText 标题动效的修改文件、启动命令和验收结果，见 [落地页导航与标题动效](LANDING_MOTION.md)。

整体检查后新增的额度刷新、历史清空确认、后台状态用词、切页体验及短期访问凭证，见 [本轮交付说明](EXPERIENCE_RELIABILITY.md)。

React 19 + TypeScript + Vite + React Router + Tailwind CSS v4（`@tailwindcss/vite`）。
共享状态通过 React Context + `useSyncExternalStore` 订阅；Axios 统一响应与错误处理；Markmap 按需加载。
Vitest 验证请求契约、任务追踪及 React 页面交互。
无示例数据注入生产代码，所有业务操作调用现有 `/api/v1`。

最新一轮的缓存恢复、登录连续性、长文稿工具、手机导航与首屏分包优化，见 [前端优化交付与验收说明](FRONTEND_OPTIMIZATION.md)。

桌面正文、操作入口及辅助文字的字号、对比度和行高调整，见 [桌面字体交付与验收说明](DESKTOP_TYPOGRAPHY.md)。

## 启动

需要 Node.js 20.19+ 或 22.12+ 及 pnpm；本次使用 Node 26、pnpm 10.32.1。

```sh
cd frontend
pnpm install --frozen-lockfile
cp .env.example .env
pnpm dev
```

打开 <http://127.0.0.1:5173>。默认 API 配置：

```dotenv
VITE_API_BASE_URL=http://localhost:8000/api/v1
```

修改前端环境变量后重启 Vite；生产环境需重新构建，变量会编译进浏览器资源。
`VITE_` 变量只可放公开的 API 地址，禁止放 ASR / LLM 密钥。

后端 PostgreSQL / Redis 就绪后，在独立终端中：

```sh
cd backend
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

再开一个终端：

```sh
cd backend
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

后端默认 CORS 白名单为 `http://localhost:5173`、`http://127.0.0.1:5173`。
更换前端端口或域名时，在后端进程环境 / 根目录 `.env` 配置 JSON 数组并重启 API：

```dotenv
CORS_ORIGINS=["https://your-frontend.example"]
```

不启用通配来源或跨域 Cookie。只允许 GET / POST / OPTIONS，公开 `X-Request-ID`。
已有 `.env.example` 与 `.env.docker.example` 增补了同名配置。

## 浏览器交互验收

1. **首页与错误**：检查黑白顶栏、标题、输入框与三张卡片。提交 `http://127.0.0.1/private`，应在输入框下看到 `SSRF_BLOCKED` 与请求编号；原链接不丢失，重试按钮可用。页脚每 15 秒检查就绪状态，依赖异常时不伪报绿灯。
2. **解析与刷新**：粘贴公开免登录的视频链接，点击“开始解析”；成功进入 `/video/{platform}_{platform_video_id}`。检查封面、平台、时长、上传者与源中实际存在的档位。刷新应保留工作台。首次访问无本地元数据的详情 URL 时，提供返回首页重新解析入口。
3. **下载与保存**：选择清晰度，检查任务从排队到执行的进度、阶段和百分比。完成后点击“保存到本地”，浏览器通过后端受控文件接口保存。MP3 档位及“提取纯音频 MP3”均可独立验证。
4. **取消**：在仍执行的下载或提取任务上点击“取消任务”。取消请求处理中按钮禁用；终态取消后停止追踪并显示错误码。若后端任务已完成，则保持完成与保存入口，不强制覆盖成取消。
5. **字幕与转写**：点击“获取字幕 / 转写”。检查来源徽章、原始段落和时间戳；有平台字幕时显示“基于平台字幕”，ASR 返回时显示“基于语音转写”。需要转写但无字幕时，按阶段 5 配置 ASR；无 Key 不自动伪造文稿。
6. **结构化总结**：点击“生成 AI 总结”，允许自动先获取转写。完成后检查全文概述、核心要点和 `[MM:SS]` 章节卡片；一小时以上使用 `H:MM:SS`。切换“查看原始文稿”。本地 heuristic 模式明确显示“本地摘录模式”。
7. **思维导图**：点击“生成思维导图”。检查 SVG 节点、滚轮缩放、拖动、节点展开；依次测试放大、缩小、适应画布、折叠与展开右栏。导出 `.md` 与 `.svg`，确认下载文件可打开。优先渲染后端 `markdown_export`，缺失时将 JSON 树转换为转义的 Markdown；关闭 raw HTML。
8. **任务历史**：打开顶栏抽屉，检查服务器最近任务、分页、进度、取消及历史保存入口。Escape / 关闭按钮可关闭，焦点返回触发器。刷新后恢复未结束任务追踪；切换视频也不会取消后台追踪。抽屉底部可清理过期文件并展示释放字节数。
9. **SSE 与回退**：在浏览器网络面板屏蔽 `/tasks/*/events` 后创建任务。应约每 1.5 秒 GET 查询，终态后停止；恢复 SSE 后创建新任务，使用命名 `progress` 事件。重复/乱序 `seq` 由单元测试验证。SSE 终态后额外 GET 完整结果，失败会重试，不丢失 `file_id/result_id`。
10. **响应式**：在 1440px 检查三栏；中等屏幕导图移至下方；390px 切换顶部“视频与下载 / 文稿与总结 / 思维导图”三个 Tab，检查无横向页面溢出，导图画布显示正确。
11. **异常与重试**：停止后端，确认网络错误文案、原输入保留及状态指示变化；恢复后重试。结果查询失败可点击“重新加载结果”；任务追踪暂时失败会继续轮询，不会伪造完成状态。

真实平台解析与下载取决于网络、平台限制和资源是否仍公开可用。AI 云调用也需后端配置对应密钥。
本阶段浏览器成功流程使用独立临时 API 夹具验证（不进入前端代码）；真实后端另验证了健康检查与 SSRF 错误。

## 测试与构建

```sh
pnpm run type-check
pnpm test:unit
pnpm run build
pnpm preview
```

Vitest 覆盖：SSE 严格版本去重、四种终态关闭、同序号终态结果读取、断线 1.5s 轮询、
GET 失败重试、404 停止、错误任务 ID、订阅清理、Redis 降级 `seq=0`、响应解包、错误规范化、
请求体契约、受控文件地址、树转 Markdown、视频元数据持久化、重复任务防护及入队失败重试。

`dist/` 包含公开页面的独立 HTML；原解析入口为 `/app`，`/video/*` 等产品页面回退到 `app-shell.html`，不存在的路径保留 404。
服务器配置参考 `backend/deploy/openresty-api.conf`，不要将全部路径回退到首页。生产 API 使用 HTTPS，并配置对应 CORS 来源。
落地页、SEO/GEO、原创素材与验证说明见 [LANDING_PAGE.md](./LANDING_PAGE.md)。
Markmap 解析器与渲染器分包，仅在导图生成后加载。

## 文件清单

- 工程：`package.json`、`pnpm-lock.yaml`、`vite.config.ts`、`tsconfig.json`、`index.html`、`.gitignore`、`.env.example`。
- 应用与页面：`src/main.tsx`、`src/App.tsx`、`src/style.css`、`src/views/HomeView.tsx`、`VideoWorkspaceView.tsx`。
- 契约与请求：`src/types/api.ts`、`src/api/client.ts`、`src/api/index.ts`。
- 状态与追踪：`src/stores/workspace.ts`、`src/lib/taskTracker.ts`、`src/hooks/useWorkspace.tsx`、`src/hooks/useWorkspaceResults.ts`。
- 组件：`AppHeader.tsx`、`AppFooter.tsx`、`TaskHistory.tsx`、`TaskCard.tsx`、`MindMapViewer.tsx`、`ErrorNotice.tsx`、`Icon.tsx`。
- 工具与测试：`src/utils.ts`、`src/tests/client.test.ts`、`taskPoller.test.ts`、`workspace.test.ts`、`reactApp.test.tsx`。
- 阶段 8 历史后端配套修改（本次迁移未修改后端）：`backend/app/core/config.py`、`backend/app/main.py`、`backend/tests/test_health.py`，仅新增可配置 CORS 与回归测试。
- 阶段 8 历史根目录配套修改：`.env.example`、`.env.docker.example` 增补 CORS 示例。

## 阶段 8：服务器历史与过期清理

历史通过 `GET /api/v1/tasks?limit=20&offset=0` 读取，浏览器仅做缓存；
打开抽屉或刷新列表会与服务器合并记录，活动任务继续使用统一 SSE / 轮询追踪。
支持加载更多、创建时间、取消和有效文件保存，文稿 / AI 结果可跳回对应工作台。
本地没有视频缓存时，从历史元数据恢复页面；下载档位可点击“重新解析可用下载档位”更新。

清理调用 `POST /api/v1/system/cleanup`，仅处理过期媒体、终态临时目录和过期孤立文件；
有效文件与运行任务受到保护，返回 `freed_bytes` 后刷新下载入口。
正在下载时可能返回 `STORAGE_BUSY`，下载完成后再点即可。

推荐在项目根目录执行 `./scripts/dev.sh` 统一启动，确保 Solo Worker 不被遗漏。
完整 21 项验收清单见根目录 README。


## React 迁移说明（2026-10-22）

- 所有 Vue 单文件组件迁移为 TSX，移除 Vue、Vue Router、Pinia、vue-tsc 与 Vue Vite 插件。
- 保留首页、三栏工作台、移动端分区、下载、音频、文稿、总结、导图、历史、清理及错误重试。
- 保留 `/api/v1`、文件下载 URL 和 `videohub.videos.v1` / `videohub.tasks.v1` 缓存格式，已有记录可直接恢复。
- 任务追踪抽取为不依赖界面框架的 `src/lib/taskTracker.ts`；SSE 去重、轮询回退与终态完整结果查询规则保持一致。
- React effect 清理关闭订阅、定时器和导图实例；结果请求在离开页面时中止，迟到的历史响应不会在应用卸载后重新启动订阅。
- React 交互测试覆盖解析与刷新、错误重试、SSE 进度与保存、取消、总结与文稿、移动端 Tab、历史清理、焦点恢复、Strict Mode 和迟到响应。
- 本次迁移只替换前端框架；登录注册、充值与管理端尚未实现。

验收记录：前端 32 项测试、TypeScript 类型检查、生产构建通过；后端 `uv run pytest` 193 项通过。
浏览器验收使用独立临时 API 夹具验证成功流程，夹具不进入生产代码；正常开发服务仍连接原后端。
浏览器另验证原有 Vue 缓存可在 React 中恢复。真实平台与付费 AI 调用未在本次迁移重新验证。

修改文件分组：

- 工程依赖与入口：`package.json`、`pnpm-lock.yaml`、`tsconfig.json`、`vite.config.ts`、`index.html`、`src/main.tsx`、`src/App.tsx`。
- 页面与组件：`src/views/*.tsx`、`src/components/*.tsx`，删除对应 `.vue` 文件与旧 `src/main.ts`。
- 状态与异步：`src/stores/workspace.ts`、`src/hooks/*`、`src/lib/taskTracker.ts`、`src/api/index.ts`，删除旧 Vue composable。
- 回归与文档：`src/tests/workspace.test.ts`、`src/tests/taskPoller.test.ts`、`src/tests/reactApp.test.tsx`、本文件与根目录 README。

启动和验证命令沿用上文。后端运行时可通过 `curl --max-time 2 http://127.0.0.1:8000/api/v1/health/live`
验证存活，在 `http://127.0.0.1:8000/docs` 检查 API；打开 `http://127.0.0.1:5173` 按浏览器交互验收逐项验证。

## 首页视觉调整：第一步

页面、导航和功能卡片使用纯白背景，功能卡片边框为 `#E5E7EB`。
主标题保持原有响应式字号，两部分均为深黑；副标题 18px / `#4B5563`，
功能说明 15px，解析按钮文字桌面 15px、手机 14px。

- 任务历史为顶部主要入口；登录后的昵称、关注状态和退出操作位于头像菜单。
- 空输入解析会提示“请先粘贴视频链接”并聚焦输入框。
- 缓存清理移至 `/settings`；`/admin` 仅向管理员展示原有健康检查面板。
  这是页面层的显示控制，原有后端健康接口的公开语义保持不变。
- 页脚提供 `/help`、`/privacy` 和 `/settings` 的有效入口，普通页面不再轮询服务状态。
- 本步未添加结果示例，也没有提交真实视频执行下载、转写或 AI 处理。

修改文件：`src/App.tsx`、`src/style.css`、`src/components/{AppHeader,AppFooter,TaskHistory,Icon}.tsx`、
`src/views/HomeView.tsx`、`src/tests/reactApp.test.tsx`、本 README。
新增 `src/views/{SettingsView,AdminView,InfoView}.tsx`。

在 `frontend/` 启动和检查：

```sh
pnpm run dev
pnpm run type-check
pnpm run build
pnpm run test:unit
```

浏览器验收：首页空输入点击解析、打开任务历史、登录后打开头像菜单、从页脚进入设置/帮助/隐私。
管理页只有管理员账户展示服务状态。375px / 320px 宽度已检查无横向溢出。
后端接口未修改；从 `backend/` 使用测试默认配置执行回归（不覆盖本机 `.env`）：

```sh
AUTH_REQUIRED=False WECHAT_MP_STATIC_QR_URL= uv run pytest
```

API 和 Worker 启动及 Swagger / curl 流程沿用 [后端交付说明](../backend/STAGE9.md)。

本次检查：前端类型检查、生产构建通过，45 项前端测试通过；后端默认测试配置下 210 项通过。

## 首页平台标识滚动带

首页解析区与功能卡片之间接入用户提供的 [React Bits LogoLoop](https://reactbits.dev/animations/logo-loop)
TypeScript + Tailwind 源码。品牌图标使用 `react-icons/si`，按需打包，不请求外部图片。

- 直线布局、黑白配色、速度 30 px/s、标识高度 28px、间距 48px、两端渐隐。
- 悬停暂停；首页不展示手动暂停按钮。
- 开启 `prefers-reduced-motion: reduce` 时替换为完整的静态列表；重复动画副本不进入无障碍树。
- 页面卸载时销毁动画引擎、监听器与观察器。
- YouTube、bilibili、Vimeo、Dailymotion、TikTok 均对应已安装 yt-dlp 的命名提取器。
  本次为前端集成与离线能力核对，没有对各平台真实视频逐个联调；单个资源仍以实际解析结果为准。

本次文件：新增 `src/components/{LogoLoop,PlatformLogos}.tsx`、`src/tests/logoLoop.test.tsx`；
修改 `src/views/HomeView.tsx`、`src/style.css`、`package.json`、`pnpm-lock.yaml` 和本 README。

使用上文 `pnpm run dev` 启动：首页可直接查看滚动带；鼠标移入观察暂停，移出继续，
系统启用“减少动态效果”后应显示静态平台列表。
后端接口与 Swagger / curl 验证流程保持不变。

本次集成验收：类型检查与生产构建通过，47 项前端测试通过，后端默认配置下 210 项通过。
浏览器已检查桌面循环/悬停暂停及 375px 手机宽度无横向溢出。

## 首页 AeroShards 背景

首屏采用用户提供的 React Bits AeroShards：400 片浅灰玻璃碎片、低速流动、两侧遮罩留白，关闭鼠标聚拢与点击脉冲，不展示暂停按钮。`HeroBackground.tsx` 负责按需加载、手机（<=767px）和减少动态效果模式下不挂载、WebGPU 初始化失败时回退纯白。

`AeroShards.tsx` 使用 vgpu 0.6；原内联着色器拆至 `components/aero-shards/*.wgsl`，由 `@vgpu/wgsl/loader-vite` 在构建期生成类型化着色器，避免在浏览器解析反射信息。页面不可见时停止渲染，组件卸载释放 GPU 资源。依赖中的 Node GPU 适配器安装脚本无需启用，浏览器使用原生 WebGPU。

本次文件：`HomeView.tsx`、`HeroBackground.tsx`、`AeroShards.tsx`、`components/aero-shards/*.wgsl`、`src/wgsl.d.ts`、`style.css`、`vite.config.ts`、`package.json`、`pnpm-lock.yaml`、`tests/heroBackground.test.tsx` 和本说明。

验证：`pnpm type-check`、`pnpm build`、`pnpm test:unit`（51 项通过）；浏览器检查桌面背景、空输入聚焦与提示，以及 390px 手机端纯白降级。启动仍使用 `pnpm dev`，访问 <http://127.0.0.1:5173>；本次为前端展示修改，无新增 HTTP 接口。

首页在 LogoLoop 后结束，不展示功能卡片、底部标语和页脚；其他页面继续保留帮助与设置页脚入口。

## 视频工作台参考图改版

工作台默认打开“视频下载”，三个标签在桌面和手机端均可切换，支持方向键、Home/End。下载面板使用左侧封面/来源信息、右侧下载设置的布局，手机端转为单栏。封面播放入口打开原平台页面，不伪装站内播放器。

清晰度和容器仅取自解析接口的真实 `download_options`；选择档位不创建任务，点击“下载视频”才提交所选 `preset_id`。格式下拉框统一选择视频容器或 MP3 音频，MP3 使用既有 `audio_extraction` 流程并隐藏清晰度。没有视频档位时禁用视频下载；未知文件大小明确显示“大小待计算”。任务进度、取消、结果保存、字幕、总结和导图逻辑沿用原接口。

修改文件：`src/views/VideoWorkspaceView.tsx`、`src/components/DownloadSettings.tsx`、`AppHeader.tsx`、`AppFooter.tsx`、`Icon.tsx`、`src/style.css`、`src/tests/reactApp.test.tsx`、`src/tests/downloadSettings.test.tsx` 与本说明。

启动：在 `frontend/` 执行 `pnpm dev`，从首页解析公开视频进入 `/video/:id`，检查清晰度选择、格式切换、视频/MP3 音频任务及三个标签。API/Worker 启动命令见前文；Swagger `/docs` 可验证现有 `/api/v1/videos/parse`、`/downloads`、`/tasks/{id}`，本次无新增接口。

验证：类型检查、构建及 53 项前端测试通过，浏览器检查桌面双栏、390px 手机单栏无横向溢出、标签键盘切换与音频模式。布局截图使用独立临时预览数据，未写入产品记录，也不代表真实视频下载已经验收。

工作台已压缩标题与标签导航，删除副标题、来源图标和底部跨标签提示；下载格式使用紧凑下拉框，不再展示独立的视频/仅音频切换或音频介绍卡片。

格式下拉箭头向内缩进 10px，保留原生下拉框的键盘操作。总结复制成功后在按钮上方显示约 2 秒的“复制成功”提示；剪贴板写入失败显示“复制失败，请重试”。提示通过 `role=status` 宣告，计时器在组件卸载时清理。类型检查、构建和 53 项前端测试通过，已有工作台测试覆盖复制 Promise 完成前不误报成功及复制失败反馈。

任务历史支持每条记录“删除”和“一键清除”。操作仅影响当前用户历史可见性，不取消任务或删除文件；清除覆盖服务器全部分页。按钮在请求期间禁用，失败显示错误并保留列表；持久化本地隐藏状态并丢弃过期刷新响应，运行任务继续跟踪。后端需先执行 Alembic 0005 迁移。前端类型、构建及 56 项测试通过。

## 首页登录拦截与 StarBorder 登录入口

首页允许未登录用户先输入和粘贴链接，提交有效链接或打开任务历史时再检查登录。登录完成后保留链接，由用户再次点击“开始解析”；关闭卡片恢复焦点时不会再次自动弹出。活跃账户取关后仍可访问已有结果和付费额度，免费新下载由后端检查关注状态。凭证失效时清除凭证并提示登录；账户恢复中的业务提交暂时禁用，网络恢复失败显示“重新连接”，不会自动弹出新的登录卡片。

顶栏将“关注公众号登录”改为“登录”，使用用户提供的 React Bits `StarBorder.tsx`（浅色、黑色流光、4 秒一圈），保留现有二维码与口令登录流程。动画在标签页隐藏时暂停，减少动态效果模式使用静态边框；旧浏览器缺少 Canvas/Observer 时仍保留可点击登录按钮。

修改：`StarBorder.tsx`、`AppHeader.tsx`、`TaskHistory.tsx`、`HomeView.tsx`、`App.tsx`、`stores/auth.ts`、`api/client.ts`、`style.css`、对应前端测试和本说明。启动仍为 `pnpm dev`；无新增接口，在首页分别点击三个入口验证卡片，再登录验证正常解析与历史访问。

## 订阅与额度（2026-10-24）

订阅页入口为 `/subscription`，在顶栏、账户菜单和页脚均可进入。
套餐卡片使用用户提供的 React Bits PixelCard，沿用当前黑白配色；支持键盘焦点、降低动态效果偏好、离屏 / 后台暂停。
没有新增依赖。价格、分钟、次数及限制来自 `GET /api/v1/billing/catalog`，失败时显示错误与重试，不使用示例套餐代替接口。

- 免费体验 30 分钟 / 5 次 AI；月会员 ¥19.90，300 分钟 / 100 次；加购包 ¥9.90，120 分钟或 100 次，均按后端策略展示。
- 登录后显示可用及执行中占用额度、会员有效期、当日下载 / 流量、额度批次到期时间；退出或换账号立即隔离之前的数据。
- 下载、音频提取、转写、总结、导图均先调用 `/billing/quotes` 展示最高消耗与资源限制，确认后仅提交 `quote_id` 到 `/billing/tasks`。
- 报价过期需重新获取；取消确认不创建任务；网络失败保留原报价重试，后端保证同一报价幂等；提交中禁用按钮。
- 额度不足可确认排队，排队不占额度；任务卡显示等待原因并提供额度入口。补充额度后可点击“重新检查”。终态和取消后刷新余额。
- 总结与导图组合成功后同时加载两种结果；自己的已有结果、导图交互和导出免费。
- 活跃账号取关后仍能访问已获得结果及付费额度；免费新下载由后端校验关注状态，权益不足不会强制退出登录。
- 当前购买按钮仅说明“暂未开放在线购买”，没有支付请求、浏览器发放入口或模拟购买成功。权益发放仍按[服务端说明](../backend/ALLOWANCES_V1.md)。

启动：已有旧 API / Worker 需先正常停止并重启，避免订阅接口 404 或 Worker 沿用旧规则。
已有依赖无需重新安装，可在项目根目录运行 `./scripts/dev.sh --skip-install`；迁移由脚本执行。
也可分别按本文开头命令运行 API、唯一一个 Solo Worker 与 Vite。

验收：

```sh
curl --max-time 5 http://127.0.0.1:8000/api/v1/billing/catalog
curl --max-time 5 http://127.0.0.1:8000/api/v1/health/ready
cd frontend
pnpm run type-check
pnpm test:unit
pnpm run build
cd ../backend
uv run pytest
```

浏览器访问 <http://127.0.0.1:5173/subscription>：检查三种套餐、加购切换、购买说明、登录后的余额与有效期。
在视频工作台点击任一操作，应先显示“确认本次额度”；点“暂不开始”不创建任务，确认后可从历史继续查看。
可在 Swagger `/docs` 依次核对 `billing/catalog`、`billing/allowances`、`billing/quotes`、`billing/tasks`；鉴权沿用公众号登录流程。
不要为了验收随意确认真实视频的付费模型任务。

本阶段验证：前端 **72 项通过**，类型检查和生产构建通过；后端 **273 项通过**。
真实本地 API 已验证套餐响应；浏览器验证了 1440px 三列、390px / 375px 单列和页面无横向溢出，以及加购切换与购买说明。
任务计费、取消与身份切换使用自动化测试；没有调用付费 ASR / LLM 或接入真实支付。

修改文件清单（相对于 `frontend/`）：

- 新增：`src/components/PixelCard.tsx`、`src/components/AllowanceConfirm.tsx`、`src/views/SubscriptionView.tsx`、
  `src/stores/billing.ts`、`src/types/billing.ts`、`src/utils/billing.ts`、`src/subscription.css`、
  `src/tests/billingFixtures.ts`、`src/tests/subscription.test.tsx`。
- 更新：`src/App.tsx`、`src/main.tsx`、`src/api/index.ts`、`src/api/client.ts`、
  `src/components/AppHeader.tsx`、`src/components/AppFooter.tsx`、`src/components/Icon.tsx`、`src/components/TaskCard.tsx`、
  `src/views/VideoWorkspaceView.tsx`、`src/hooks/useWorkspaceResults.ts`、
  `src/stores/auth.ts`、`src/stores/workspace.ts`、`src/types/api.ts`、
  `src/tests/reactApp.test.tsx`、`src/tests/auth.test.tsx`、`src/tests/client.test.ts`、本文。


## 视频问答与个人知识库（2026-10-24）

工作台新增“视频问答”Tab：使用已经获得的文稿独立提问，先展示额度报价，再由 TaskService / Celery 执行。新问题成功消耗 1 次 AI，不重复扣转写分钟；无依据、失败或有效取消不扣次数。相同文稿、模型及版本的已有回答免费。

回答附原文片段及时间范围；ASR 时间标记“约”。支持查看对应文稿、打开原视频；历史问答可从任务抽屉继续查看。
文稿与总结可收藏并编辑标签；`/library` 支持自己的标题 / 内容 / 标签搜索、分页、阅读及提问。收藏与阅读免费，不依赖媒体文件是否过期。
总结新增结论、适用人群、行动步骤与经过原文匹配的引用；旧结果兼容，有依据才填新字段。

启动命令沿用本文；需先升级后端到 `0007` 并重启 API 与唯一的 Solo Worker。
模块边界、接口、curl / Swagger 验收、修改清单与限制见 [知识工作台交付说明](../backend/KNOWLEDGE_V1.md)。
浏览器成功数据使用独立 schema 的离线样本，未调用付费模型，样本不进入业务数据。

本阶段最终验证：前端 **79 项通过**、后端 **284 项通过**，类型检查、生产构建与迁移一致性检查通过。独立离线浏览器样本已验证工作台引用跳转及 375px 无横向溢出；预览不代表真实云模型效果。

## 备案期间的服务器版本内部预览

前提：Mac的SSH隧道127.0.0.1:18000 → 服务器127.0.0.1:8000保持打开，服务器端已成功部署。此模式不会开放公网网站或修改生产CORS。

在项目根目录下载当前部署的前端静态文件（不会下载任何.env）：

```sh
mkdir -p .runtime/server-preview
scp -i ~/.ssh/videohub_actions -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -r videohub-deploy@129.28.26.221:/opt/videohub/current/frontend/dist .runtime/server-preview/
```

然后在frontend目录启动本机预览：

```sh
node node_modules/vite/bin/vite.js preview --config vite.server-preview.config.ts
```

打开http://127.0.0.1:5175，静态页面来自下载的部署版本，/api/v1请求经过本地代理与SSH隧道到真实服务器；SSE及下载也沿同一路径。Ctrl+C停止预览，SSH窗口需另行Ctrl+C关闭。刷新/subscription等路由由Vite预览提供SPA回退。

这是内部测试工具，不用于公网生产托管。真实任务使用服务器账号和额度，可能调用付费模型；仅打开页面不创建AI任务。公众号公网回调仍需原有可用回调配置，隧道不会把本机18000端口暴露给微信，不提供生产mock登录绕过。服务器新版部署后重新下载静态文件再启动预览；如需核实版本，可读取服务器current/release.json的commit。下载缓存位于被Git忽略的.runtime，不要提交。

## 管理后台前端（2026-10-26）

`/admin` 已接入概览、用户管理、会员与额度、任务管理、操作审计和存储与服务六个入口。
沿用 React、TanStack Query、Radix Dialog/Dropdown Menu 和独立样式，后端保持模块化单体。
账号停用/恢复、固定产品人工发放、协作取消和过期文件清理均接入原因、明确确认、原编号核对及审计回执。
`/settings` 只向活跃管理员提供存储管理链接，不再直接执行全局清理。

本地源码验收：**http://127.0.0.1:5176/admin**，配套 **18766 临时数据后端**，不连接生产。
启动命令、修改清单、curl/Swagger 验证和最终结果见 [阶段 4–5 交付与最终检查](ADMIN_FRONTEND_FINAL.md)。
前端 160 项、后端 338 项和 31 个隔离 HTTP 检查通过；类型检查与构建通过。
真实浏览器视觉验收受工具已保存的访问权限限制尚未完成；生产未部署。
5175 仍是上文服务器发布版本预览的独立方案，本轮没有启动该预览或 SSH 隧道。

历史阶段记录：[阶段 1](ADMIN_FRONTEND_STAGE_1.md)、[阶段 2](ADMIN_FRONTEND_STAGE_2.md)、[阶段 3](ADMIN_FRONTEND_STAGE_3.md)。

2026-10-27 根据手测完善加载、弹窗居中、列表摘要与快捷筛选，并修复分页恢复、刷新失败保留和焦点问题。前端现为 **171 项通过**；详见 [交互完善与验证](ADMIN_UI_REFINEMENT.md)。


## 思维导图配色与直角连线（2026-10-31）

已有导图现在按一级分支着色，用字号、背景和线宽区分层级，连接线改为直角折线。保留缩放、平移和展开收起，并支持键盘操作；SVG 导出保留配色和当前展开内容，不受屏幕缩放裁切影响。无需重新生成导图或消耗额度。

修改清单、启动命令、curl / Swagger 验证、格式说明与测试结果见 [导图交付说明](MINDMAP_STYLE.md)。本轮前端 232 项、后端 360 项测试通过，类型检查与生产构建通过。


## 首页输入框与广告占位（2026-10-31）

`/app` 的链接输入区改为白底、细灰边框和一体式圆角布局；输入框自身不再显示绿色焦点边框，由外框提供中性的灰色聚焦反馈。按钮的圆角、尺寸及禁用态同步优化；手机端使用输入行与全宽按钮两行布局。链接输入关闭自动大写、自动纠正和拼写检查，原有校验、登录、提交和失败重试逻辑保持。

静态广告展示位固定贴在视口底部，最大宽度 560px，桌面高度 88px、手机高度 72px（另计设备安全区），右上角提供 X 关闭按钮。页面预留底部空间避免遮挡内容，关闭广告时同步释放；刷新页面后恢复展示。当前没有广告 SDK、外部请求或点击跳转。

主内容在页面就绪后自动获焦产生的蓝色轮廓已移除，保留原有焦点管理以及输入框、按钮的键盘焦点提示。登录状态恢复失败提醒改为浅橙背景、橙色边框及警告图标，重连按钮使用同色系。提醒固定在右上方，最大宽度 420px，层级高于普通导航和正文，不再占用页面布局；手机端按屏幕宽度及安全区收窄。右上角 X 可关闭当前提醒，关闭不清除登录错误或改变操作权限；重新尝试恢复后如仍失败会再次提醒。短暂淡入动效遵守系统减少动态效果设置。

修改文件：`src/views/HomeView.tsx`、新增 `src/home.css`、`src/PublicApp.tsx`、`src/components/Icon.tsx`、`src/workspace.css` 及本文。在 `frontend/` 执行 `npm run dev`，访问 http://127.0.0.1:5173/app；用鼠标和 Tab 检查输入及按钮焦点，检查空链接、非法链接、登录与解析流程、登录恢复失败提醒，并检查底部广告关闭和空间释放。HTTP 验证：`curl --max-time 10 -I http://127.0.0.1:5173/app`。本轮没有修改 API，Swagger 验证沿用原流程。

验证：`npm run type-check`、`npm run build`（含公开静态文档检查）及 `npm run test:unit -- src/tests/reactApp.test.tsx src/tests/auth.test.tsx src/tests/frontendExperience.test.tsx`（52 项）通过。真实浏览器截图未在本轮复核。

悬浮提醒调整复核：`npm run type-check`、`npm run build` 及 `npm run test:unit -- src/tests/reactApp.test.tsx src/tests/auth.test.tsx`（44 项）通过，`/app` HTTP 检查返回 200。手动检查提醒不推挤正文、点击 X 后消失，以及重连成功后不再显示提醒；本轮未复核浏览器截图。
