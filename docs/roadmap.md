# 开发路线图

按阶段推进，每个阶段遵循同一节奏：实现 → 测试 → 验收 → git commit → 进入下一阶段。任何阶段不允许跳过验收直接进入下一阶段；发现上一阶段的问题，回到上一阶段修，不在新阶段里「顺手改」。

## 阶段总览

| 阶段 | 内容 | 关键产出 |
|------|------|----------|
| 0 | 文档与仓库初始化 | 本套文档 + AGENTS.md + git 仓库 |
| 1 | 项目骨架 + 可观测性 | docker compose 全栈可启动 |
| 2 | 数据库层 | models + migration + seed |
| 3 | 下载核心链路 | URL → 解析 → 下载 → 文件 |
| 4 | 用户系统 + 限流 | 注册登录 + 权限服务 + 配额 |
| 5 | 前端核心页 + 联调 | 第一个端到端可用版本 |
| 6 | 会员与订单 | 套餐、订阅、权益生效 |
| 7 | AI 功能 | 音频、转写、总结、思维导图 |
| 8 | 支付 | 订单 → webhook → 开通订阅 |
| 9 | 管理后台 + 安全加固 | 运营看板 + 上线前审计 |
| 10 | Ask Video（可选）+ 上线 | pgvector 问答 + 部署 |

## Phase 0 — 文档与仓库初始化

产出：`AGENTS.md`、`docs/architecture.md`、`docs/database.md`、`docs/api.md`、`docs/roadmap.md`，git 仓库初始化。

验收：文档评审通过，四份文档之间无互相矛盾的定义（状态机、错误码、表名三处交叉核对）。

## Phase 1 — 项目骨架 + 可观测性

范围：frontend（Vite + React + TS + Tailwind + shadcn/ui）、backend（FastAPI + uv 管理依赖）、worker/beat 进程入口、docker-compose（postgres、redis、backend、worker、beat、flower、frontend）、环境变量模板 `.env.example`、ESLint/Prettier/ruff/mypy 配置、`/healthz` 与 `/readyz`。

可观测性在本阶段完成而不是后补：structlog JSON 日志 + request_id 中间件、Sentry 三端接入（api / worker / frontend，DSN 走环境变量，未配置时降级为关闭）、Flower 随 compose 启动。

验收：`docker compose up` 后全部服务健康；访问 `/readyz` 返回 DB/Redis 连通；前端 dev server 可访问；刻意抛一个异常能在 Sentry 看到（DSN 配置时）。

## Phase 2 — 数据库层

范围：按 `docs/database.md` 实现 13 张表的 SQLAlchemy models、Alembic 异步迁移环境、初始 migration、plans seed 脚本（free / monthly / yearly）。

验收：`upgrade head → downgrade -1 → upgrade head` 可逆执行；`\dt` 检查表结构与文档一致；seed 后 plans 三行就绪。

## Phase 3 — 下载核心链路

范围：Downloader 抽象 + YTDLPDownloader（接口含 proxy/cookie 注入位）、URL 安全校验（SSRF 防护）、parse 流程、download 流程（FFmpeg 合并/转封装）、StorageProvider（阿里云 OSS 实现，开发环境用本地磁盘实现）、进度推送（Redis pub/sub + 节流落库 + SSE 端点）、任务取消（cancel key + 子进程终止 + 临时文件清理）、超时与重试、`cleanup_expired_files` Beat 任务。

本阶段不做用户系统：API 临时允许匿名调用，仅限本机开发使用。

验收：本地对 3 个测试 URL（不同平台、不同时长）完成「解析 → 选格式 → 下载 → SSE 进度 → 拿到文件」；中途取消一次，确认子进程被杀、临时文件被清理、状态为 cancelled；service 层单测（mock yt-dlp/FFmpeg/OSS）+ 一个真实小视频的 e2e 冒烟测试通过。

## Phase 4 — 用户系统 + 限流

范围：注册 / 登录 / refresh / users.me、bcrypt、JWT 中间件、PermissionService（当前只有 download 一个权益点）、usage_records 写入、IP 级限流（登录/注册/parse）+ 用户级配额（免费档每日次数与时长上限）、usage 查询端点。

红线：本阶段未完成、限流未生效之前，服务不得部署到任何公网环境。匿名下载能力在本阶段关闭，Phase 3 的临时匿名入口删除。

验收：未登录请求受保护端点返回 401；超配额返回 `quota_exceeded`；连续高频请求 parse 返回 429 + `Retry-After`。

## Phase 5 — 前端核心页 + 联调

范围：极简黑白首页（Logo、导航、Hero、URL 输入框、解析按钮）、解析结果卡片（标题/封面/时长/格式选择）、下载进度（SSE + 断线重连）、下载按钮、登录/注册页、历史记录页。API 层按 `api/ + hooks/ + types/` 组织，TanStack Query 管理服务器状态，页面刷新后任务状态可恢复。

验收：匿名打开首页可解析；登录后完整走通下载；刷新页面进度不丢；移动端布局可用；设计走查无渐变、无玻璃拟态、无装饰性背景。

## Phase 6 — 会员与订单

范围：plans 展示、orders 创建（金额服务端快照）、subscriptions 开通逻辑（本阶段用手动/管理后台触发，不接支付）、`expire_subscriptions` Beat 任务、PermissionService 扩展全部权益点、`features` 矩阵配置化。

验收：把某用户置为会员后，AI 端点从 403 变为可用；订阅到期任务执行后权益回收。

## Phase 7 — AI 功能

范围：processing_tasks 表落地（若 Phase 2 未含）、audio_extract / transcribe / summarize / mindmap 四个任务链、LLMProvider 抽象（一个 OpenAI 兼容实现 + 配置切换）、Markmap 前端渲染、每步调用写 usage_records（token、ASR 时长、估算成本）。

验收：对一个 10 分钟测试视频完成全链路；usage_records 每步都有记录且成本非零；重复触发同一功能返回已有产物，不重复扣配额。

## Phase 8 — 支付

范围：PaymentProvider 选型与抽象、payments webhook（验签 + `(provider, trade_no)` 幂等）、订单状态机（pending/paid/failed/expired/refunded）、支付成功事务内开通订阅、订单过期扫描。

验收：重放同一 webhook 不产生第二个 payment、不重复开通；未支付订单到期自动 expired；全链路用渠道沙箱跑通一次。

## Phase 9 — 管理后台 + 安全加固

范围：`/admin/` 看板（用户、订单、失败任务、LLM/存储成本、队列状态）；安全审计：SSRF 规则复核、安全响应头、CORS、请求体大小限制、依赖漏洞扫描。

## Phase 10 — Ask Video（可选）+ 上线

范围：pgvector 扩展 + transcript_chunks 迁移、embedding 任务、问答端点；生产 compose + Nginx（TLS、SSE 关闭缓冲）、备份策略、上线 checklist。

## 已知风险记录

以下事项经讨论决定不在当前阶段处理，记录在此避免遗忘：

- **平台可用性验证**：yt-dlp 在目标部署 IP 段对各平台的成功率是业务存活前提，须在首次公网部署前补做验证（成功率、限速、所需登录态）。
- **代理池 / Cookie 池**：Downloader 接口已预留注入位，接入时机取决于上一项的验证结果。
- **支付渠道选型**：部分渠道对本品类有准入限制，Phase 8 启动前先确认渠道再写代码。
