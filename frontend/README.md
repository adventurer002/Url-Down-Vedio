# VideoHub AI 前端 · 本地 V1（阶段 8）

Vue 3 + TypeScript + Vite + Vue Router + Pinia + Tailwind CSS v4（`@tailwindcss/vite`）。
Axios 统一响应与错误处理；Markmap 按需加载；Vitest 验证请求契约和任务追踪。
无示例数据注入生产代码，所有业务操作调用现有 `/api/v1`。

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
GET 失败重试、404 停止、错误任务 ID、作用域清理、Redis 降级 `seq=0`、响应解包、错误规范化、
请求体契约、受控文件地址、树转 Markdown、视频元数据持久化、重复任务防护及入队失败重试。

`dist/` 是静态发布产物。服务器需将 `/video/*` 回退至 `index.html`（Vue Router history 模式），
例如 Nginx `try_files $uri $uri/ /index.html;`。生产 API 使用 HTTPS，并配置对应 CORS 来源。
Markmap 解析器与渲染器分包，仅在导图生成后加载。

## 文件清单

- 工程：`package.json`、`pnpm-lock.yaml`、`vite.config.ts`、`tsconfig.json`、`index.html`、`.gitignore`、`.env.example`。
- 应用与页面：`src/main.ts`、`src/App.vue`、`src/style.css`、`src/views/HomeView.vue`、`VideoWorkspaceView.vue`。
- 契约与请求：`src/types/api.ts`、`src/api/client.ts`、`src/api/index.ts`。
- 状态与追踪：`src/stores/workspace.ts`、`src/composables/useTaskPoller.ts`。
- 组件：`AppHeader.vue`、`AppFooter.vue`、`TaskHistory.vue`、`TaskCard.vue`、`MindMapViewer.vue`、`ErrorNotice.vue`、`Icon.vue`。
- 工具与测试：`src/utils.ts`、`src/tests/client.test.ts`、`taskPoller.test.ts`、`workspace.test.ts`。
- 后端配套修改：`backend/app/core/config.py`、`backend/app/main.py`、`backend/tests/test_health.py`，仅新增可配置 CORS 与回归测试。
- 根目录配套修改：`.env.example`、`.env.docker.example` 增补 CORS 示例。

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
