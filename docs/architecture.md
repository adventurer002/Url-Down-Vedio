# 系统架构

本文档定义整个系统的技术架构、模块边界与核心流程。所有实现必须以本文档为准；需要偏离时先修改本文档并说明理由。

## 1. 系统总览

用户粘贴视频 URL，系统完成解析、下载、转码并交付文件；在此之上叠加会员权益：音频提取、字幕转写、AI 总结、思维导图，后续扩展 Ask Video。

```text
                        用户
                         │
                         ▼
                React SPA (Vite)
                         │
                  REST API / SSE
                         │
                         ▼
                  FastAPI (api)
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
    PostgreSQL         Redis         阿里云 OSS
                       │  ▲                ▲
              broker + pub/sub             │
                       ▼                   │
                Celery Worker              │
                       │                   │
        ┌──────────────┼──────────────┐    │
        ▼              ▼              ▼    │
     yt-dlp         FFmpeg      faster-    │
                     │          whisper    │
                     └──────临时文件────────┘
                                   │
                              LLMProvider
                                   │
                        OpenAI / DeepSeek / ...
```

职责划分：

- **api 进程**：无状态 HTTP 服务，只做参数校验、权限、调度与查询，不执行任何媒体处理。
- **worker 进程**：执行解析、下载、转码、ASR、LLM 调用等全部长耗时任务。
- **Redis**：Celery broker、任务进度 pub/sub、限流计数、取消信号。
- **PostgreSQL**：唯一持久化事实来源；Ask Video 阶段启用 pgvector。
- **阿里云 OSS**：全部产物文件（视频、音频、字幕）的最终存储；本地磁盘只放临时文件。

## 2. 仓库结构

Monorepo，api 与 worker 共用同一份 backend 代码库（模型、schema、service 完全共享，仅进程入口不同），避免双仓库的同步成本。

```text
video-saas/
├── frontend/
│   ├── src/
│   │   ├── api/            # 请求函数，唯一允许触碰 HTTP 的层
│   │   ├── hooks/          # TanStack Query hooks
│   │   ├── types/          # 与后端 schema 对齐的类型
│   │   ├── stores/         # Zustand
│   │   ├── components/     # shadcn/ui 与业务组件
│   │   └── pages/
│   └── package.json
├── backend/
│   ├── app/
│   │   ├── api/            # 路由层（v1）
│   │   ├── core/           # config、security、logging、exceptions
│   │   ├── models/         # SQLAlchemy models
│   │   ├── schemas/        # Pydantic schemas
│   │   ├── services/       # 全部业务逻辑
│   │   ├── providers/      # Downloader / LLM / Payment / Storage 抽象
│   │   └── tasks/          # Celery tasks（薄封装，调用 services）
│   ├── alembic/
│   ├── tests/
│   └── pyproject.toml
├── docker-compose.yml
├── AGENTS.md
└── docs/
```

关键依赖倒置规则：`api` 依赖 `services`，`services` 依赖 `providers` 的抽象接口，`providers` 的实现（yt-dlp、OpenAI、Stripe、S3）位于依赖最外层。替换任何外部供应商只改 providers 层。

## 3. Provider 抽象

| 抽象 | 首批实现 | 预留 |
|------|---------|------|
| `Downloader` | `YTDLPDownloader` | `HLSDownloader`、平台专用 Downloader |
| `LLMProvider` | 一个 OpenAI 兼容实现 | DeepSeek、Claude 等，配置切换 |
| `PaymentProvider` | 待定（Phase 8 前选型） | Stripe / 微信支付 / 支付宝 |
| `StorageProvider` | 阿里云 OSS（oss2 SDK） | 本地磁盘实现（仅开发环境） |

`Downloader` 接口约定：

```python
class Downloader(Protocol):
    async def extract_info(self, url: str) -> VideoInfo: ...
    async def download(
        self,
        url: str,
        format_id: str,
        dest_dir: Path,
        on_progress: Callable[[DownloadProgress], None],
        should_cancel: Callable[[], bool],
        proxy: str | None = None,
        cookie_file: Path | None = None,
    ) -> Path: ...
```

`proxy` 与 `cookie_file` 从第一天就出现在接口里（当前阶段调用方传 `None`），后续接入代理池、账号池时不需要改调用链。

## 4. 核心流程

### 4.1 视频解析

```text
POST /videos/parse {url}
  → URL 安全校验（协议白名单、长度、SSRF 检查，见 §8）
  → 创建 video 记录（status=parsing）
  → 投递 Celery: tasks.parse_video
  → 前端持有 video_id，订阅 SSE 或轮询
worker:
  → Downloader.extract_info(url)
  → 写回 title / uploader / thumbnail / duration / formats / raw_info
  → video.status = ready（失败则 failed + error_message）
```

解析也走 Celery 而不是同步请求内完成：受限平台上 yt-dlp 提取信息可能耗时 5–30 秒，同步等待会拖垮 api 进程。parse 任务设置 60 秒 `time_limit`。

### 4.2 视频下载

```text
POST /downloads {video_id, format_id}
  → PermissionService.can_download()
  → 配额检查（usage_records 统计当日用量）
  → 创建 download_task（status=queued）
  → 投递 Celery: tasks.download_video
worker:
  → Downloader.download(...)          # 产出视频/音频流文件
  → FFmpeg 合并音视频 / 转封装         # 需要时
  → StorageProvider.upload(tmp_file)  # 上传 S3，创建 media_files 记录
  → 删除本地临时文件
  → task.status = completed
```

任务状态机：

```text
queued → downloading → processing → uploading → completed
   │          │            │           │
   └──────────┴─────┬──────┴───────────┘
                    ▼
                 failed            cancelled（仅允许在 uploading 之前取消）
```

- 状态只能向前推进，不允许回退；`failed` 与 `cancelled` 是终态。
- `retry_count` 记录重试次数，超过上限（默认 2 次）后置为 `failed`。
- 失败必须写 `error_code`（机器可读）与 `error_message`（用户可读），`error_code` 取值范围在 `docs/api.md` 定义。

### 4.3 进度推送

yt-dlp 的 progress hook 在 worker 进程内，SSE 连接在 api 进程内，两者经 Redis 解耦：

```text
worker: yt-dlp progress hook
  → publish 到 Redis channel: task_progress:{task_id}
  → 同时节流写库：progress 字段每 ≥1s 才 UPDATE 一次（保证刷新页面可恢复）

api: GET /downloads/{id}/events (SSE)
  → 先回放缓存状态（从 DB 读，保证晚到的订阅者拿到当前状态）
  → subscribe channel，持续转发
  → 任务进入终态后发送 end 事件并关闭连接
```

前端断线重连时重新打开 SSE 即可，DB 里的 `progress` 兜底。

### 4.4 任务取消

```text
POST /downloads/{id}/cancel
  → DB 标记 cancel_requested
  → 设置 Redis key: task_cancel:{task_id} = 1（TTL 1h）
  → celery_app.control.revoke(celery_task_id, terminate=True)

worker 内 Downloader.download 的 should_cancel 回调：
  → 每次 progress hook 检查 Redis cancel key
  → 命中后终止 yt-dlp/ffmpeg 子进程
  → 清理 dest_dir 下临时文件
  → task.status = cancelled
```

`should_cancel` 轮询而不依赖信号：SIGTERM 在 Windows 与容器环境的子进程传播不可靠，显式检查退出点更可控。

### 4.5 AI 处理流程

四个功能共享同一条前置链，每步都是独立 Celery task，产物各自落库：

```text
audio_extract:  video → FFmpeg → MP3/M4A → S3 → media_files(kind=audio)
transcribe:     audio（或 video 提取的音轨）→ faster-whisper → transcripts
summarize:      transcripts → LLMProvider → summaries（summary/key_points/chapters/keywords）
mindmap:        summaries → LLMProvider → Markmap 兼容 Markdown → mind_maps
```

设计约束：

- 思维导图让 LLM 输出 Markdown 层级文本，前端用 Markmap 渲染，不让 LLM 直接生成图片。
- `summarize` 记录 `prompt_version`，prompt 迭代后旧结果可追溯。
- 每次 LLM/ASR 调用写 `usage_records`（token 数、ASR 时长、估算成本），成本核算是定价依据，不是可选项。

### 4.6 Ask Video（后续阶段）

启用 pgvector 扩展，transcript 分块后 embedding 入库（新增 `transcript_chunks` 表，届时单独 migration）。第一版不做独立向量数据库。

```text
question → embedding → pgvector 近邻检索 → 相关 chunk 作上下文 → LLM → answer
```

## 5. 异步任务系统

- Broker 与 result backend 均为 Redis；任务结果以 DB 记录为准，result backend 只用于 Celery 内部状态。
- 队列划分：`default`（parse）、`download`、`ai`。队列分离后可用多个 worker 或不同并发参数隔离负载，也为后续会员优先队列留位置。
- 每个 task 声明 `soft_time_limit` / `time_limit`：parse 60s，download 按配置上限（默认 30min），ASR 按时长估算上限。
- 重试策略：网络类异常指数退避（最多 2 次）；业务失败（平台拒绝、格式不存在）不重试直接 `failed`。
- Celery Beat 周期任务：`cleanup_expired_files`（每小时）、`expire_subscriptions`（每日，Phase 6 起）。

## 6. 存储方案

- 产物一律先写 worker 本地临时目录，处理完成后上传 OSS，随即删除本地文件。任何情况下本地文件都不是交付来源。
- Bucket 保持私有、不开公共读；下载交付使用 OSS 签名 URL（有效期 15 分钟），流量不经 api 进程中转。
- Endpoint、Bucket、AK/SK 走环境变量；将来若部署到同地域 ECS，切换内网 Endpoint 可省下行流量费。
- `media_files.expires_at` 是强制字段：免费用户文件 24h、会员 7 天（均为配置项）。Beat 任务到期删除 OSS 对象并把记录标记为已清理。没有 TTL 的存储方案会让成本随用户量线性失控。
- 存储键规范：`{env}/{user_id}/{kind}/{uuid}.{ext}`，禁止把原始文件名作为键。

## 7. 会员与权限

- `PermissionService` 是唯一权限判断入口，输入用户与动作，输出允许/拒绝 + 原因码。路由、task 调度前都必须经它检查。
- 权限矩阵存 `plans.features`（JSONB），改权益不改代码。
- 配额：免费用户每日下载次数、单视频时长上限由 plan 配置定义，worker 与 api 都用 `usage_records` 统计当日用量。
- 权益档位见 `docs/api.md` 的权限矩阵表。

## 8. 安全与限流

URL 安全（SSRF 防护，解析服务的高危点）：

1. 仅允许 `http`/`https` 协议；
2. URL 长度上限 2048；
3. 域名解析后校验目标 IP 不在内网与保留网段（10/8、172.16/12、192.168/16、127/8、169.254/16、::1 等）；
4. 校验在 api 层完成，worker 不信任传入 URL（worker 侧的 Downloader 调用前再校验一次）。

认证与请求安全：

- JWT access token（2h）+ refresh token（14d），密码 bcrypt 哈希。
- CORS 白名单、安全响应头、请求体大小限制。

限流（红线：未完成本项不得部署公网）：

- IP 级：登录、注册、parse 接口限频（如 10 次/分钟，配置项），429 + `Retry-After`。
- 用户级：下载与 AI 功能按 plan 配额限制，超限返回业务错误码。
- 限流实现基于 Redis 滑动窗口，中间件统一处理。

## 9. 可观测性

骨架阶段（Phase 1）就必须接入，不允许事后补：

- 日志：structlog JSON 输出，api/worker 统一格式，请求级 `request_id` 与任务级 `task_id` 贯穿。
- 异常：Sentry 同时接 api、worker、frontend 三端。
- 队列：Flower 监控 Celery（队列长度、失败率、worker 存活）。
- 健康检查：`GET /healthz`（存活）与 `GET /readyz`（DB/Redis 连通），供容器探针使用。

## 10. 部署

开发环境一套 `docker-compose.yml` 起全部依赖：

```text
postgres:16    redis:7    backend(uvicorn --reload)    worker(celery)
beat(celery beat)    flower    frontend(vite dev)
```

生产环境（MVP 阶段）单机 Docker Compose + Nginx 反代即可：Nginx 终止 TLS、反代 api 与前端静态资源、SSE 连接关闭缓冲（`proxy_buffering off`）。多机扩容留待出现真实负载后再设计。
