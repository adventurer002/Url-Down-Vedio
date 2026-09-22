# AGENTS.md — 项目开发规则

本文件是 AI 协作者（Cursor 等）在本仓库中必须遵守的约束。修改代码前先读本文件和 `docs/` 下的设计文档。

## 项目概述

商业化视频解析与 AI 视频处理 SaaS。用户粘贴视频 URL，系统解析、下载、转码；会员额外提供音频提取、字幕转写、AI 总结、思维导图，后续扩展 Ask Video。

## 技术栈（不得擅自变更）

- Frontend: React 18 + TypeScript + Vite + Tailwind CSS + shadcn/ui + TanStack Query + Zustand
- Backend: Python 3.12 + FastAPI + SQLAlchemy 2.x + Alembic + Pydantic v2 + JWT
- Infra: PostgreSQL 16 + Redis 7 + Celery 5 + Docker Compose
- Media: yt-dlp + FFmpeg（ffprobe）
- AI: faster-whisper + LLM API（供应商可替换）
- Storage: 阿里云 OSS（生产，oss2 SDK）；本地磁盘实现（仅开发环境，经 StorageProvider 切换）

## 本地环境

- 本机未安装 PostgreSQL，也没有独立服务器。开发所需的 PostgreSQL / Redis 一律由 docker compose 提供，禁止要求在本机安装数据库服务，连接配置只允许指向 compose 服务。
- 集成测试同样跑在 compose 的依赖服务上（可用独立 test 库），不依赖任何本机已安装的组件。
- 部署目标形态是单机 docker compose，不引入必须依赖云厂商专有服务才能运行的设计。

## 通用规则

- 不擅自改变 `docs/` 中已确定的架构；架构变更先改文档，再改代码。
- 不为完成单个小功能引入新框架或新依赖；新增依赖需在提交信息中说明理由。
- 优先复用已有代码，禁止复制粘贴重复业务逻辑。
- 业务逻辑全部放在 service 层。API route 只做四件事：参数校验、权限检查、调用 service、返回响应。
- 每完成一个功能：类型检查 → lint → 测试 → 更新受影响文档 → 提交（要求见「提交与交付」）。

## Backend

- 包管理使用 uv，禁止 pip/conda。
- 禁止在 FastAPI 请求处理函数中执行下载、转码、ASR、LLM 等长耗时操作，一律进入 Celery。
- SQLAlchemy 使用 2.x 风格（`select()`、`Mapped[]`、`mapped_column`），禁用 1.x 的 `query` API。
- 所有出入参用 Pydantic v2 定义，request schema 与 response schema 分开，不直接暴露 ORM 对象。

## 异步任务

- 任务状态机以 `docs/architecture.md` 为准，禁止私自增加、跳过或复用状态。
- 进度上报统一走 Redis pub/sub + 定时落库（见架构文档），worker 不得直接 HTTP 回调 API。
- 取消任务必须完成三件事：终止子进程、清理本地临时文件、把任务状态置为 `cancelled`。
- 每个 Celery task 必须显式设置 `soft_time_limit` 与 `time_limit`，重试必须带指数退避和上限。

## Downloader

- 业务代码禁止直接调用 yt-dlp，必须经 `Downloader` 抽象（`YTDLPDownloader` 等实现）。
- Downloader 实现必须支持注入 proxy / cookie 配置（当前阶段可传 `None`），为后续代理池、账号池预留接口。
- yt-dlp 版本固定在 `pyproject.toml` 中。升级 yt-dlp 必须单独提交，并运行下载冒烟测试后再合并。

## AI

- 所有 LLM 调用必须经过 `LLMProvider` 抽象，禁止在业务代码中写死 OpenAI / DeepSeek / Claude 等具体供应商。
- LLM 调用的 input/output token 用量、ASR 时长必须写入 `usage_records`，供成本核算。

## Database

- 所有结构变更必须写 Alembic migration，禁止直接改数据库。
- migration 必须可升可降，`downgrade()` 不允许为空或 `pass`。
- 所有表使用 UUID 主键 + `created_at` / `updated_at`。
- 枚举字段使用 `Enum(native_enum=False)`（varchar + check constraint），避免 Postgres 原生枚举的迁移负担。

## Frontend

- 禁止 `any`；API 请求统一走 `src/api/` 与 `src/hooks/`，组件内不直接写 fetch/axios。
- 服务器状态用 TanStack Query，客户端状态用 Zustand，二者不混用。
- 设计风格以极简黑白为基准：大量留白，不引入渐变、玻璃拟态、装饰性背景。

## Security（红线）

- 不信任前端传来的权限、价格、会员状态、支付结果。
- 支付结果以 webhook 为准：必须验签、必须幂等（重复 webhook 不产生副作用）。
- 用户提交的 URL 必须校验：协议白名单（仅 http/https）、长度限制、域名解析后校验目标 IP 不在内网/保留网段（SSRF 防护）。
- 所有功能权限判断走 `PermissionService`，禁止把会员判断散落在路由里。
- 密钥、连接串只允许从环境变量读取，禁止写进代码或提交进仓库。

## 提交与交付

- 每次改动完成后创建对应的 git commit，粒度以「一个可独立回滚的改动」为准；提交信息说明为什么改，而不是罗列改了哪些文件。
- 每次改动必须编写或更新相关测试，不允许「先实现后补测」。
- 交付给用户前必须跑完全部验证：pytest、lint、类型检查、前端测试与构建，全部通过才算完成；任何一项失败先修复再重跑，失败状态不交付。

## Testing

- service 层单测必须 mock 掉 yt-dlp、FFmpeg、LLM、OSS 等外部依赖。
- 下载链路保留一个最小 e2e 冒烟测试（使用时长小于 1 分钟的公开测试视频）。
- 前端关键 hooks 与工具函数保留单元测试。
