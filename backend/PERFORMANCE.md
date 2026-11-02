# 4 核 / 8GB / 10Mbps：第一轮性能优化

## 本轮修改文件

以下仅列本轮触及的文件；工作区原有其他改动保持原样。

```text
.env.example
.env.docker.example
backend/deploy/production.env.example
backend/app/core/config.py
backend/app/infrastructure/concurrency.py（新增）
backend/app/infrastructure/timing.py（新增）
backend/app/modules/video/parsers/cached.py（新增）
backend/app/modules/video/router.py
backend/app/modules/download/router.py
backend/app/modules/download/worker.py
backend/app/modules/media/router.py
backend/app/modules/transcript/router.py
backend/app/modules/transcript/providers.py
backend/app/modules/transcript/worker.py
backend/app/modules/ai/providers/base.py
backend/app/modules/ai/providers/bounded.py
backend/app/modules/ai/providers/openai_compatible.py
backend/app/modules/ai/router.py
backend/app/modules/ai/summarizer.py
backend/app/modules/ai/worker.py
backend/app/modules/billing/router.py
backend/app/modules/knowledge/worker.py
backend/tests/test_performance.py（新增）
backend/PERFORMANCE.md（新增）
```

## 本轮运行模型

保留一个 API 进程和一个 `--pool=solo -c 1` Worker。不同后台任务仍串行；单个任务内独立 LLM 分段最多两个同时请求。Map 全部完成后才能 Reduce，同层 Reduce 可并发，全部总结完成后才能生成思维导图。

| 设置 | 默认值 | 用途 |
| --- | --- | --- |
| `LLM_CHUNK_CONCURRENCY` | 2 | 总结 Map 和同层 Reduce 并发，允许 1–2 |
| `ASR_CHUNK_CONCURRENCY` | 1 | 千问音频分段上传并发，允许 1–2；其他 ASR 适配器不受此设置影响 |
| `VIDEO_PARSE_CACHE_TTL_SECONDS` | 120 | 视频解析结果有效秒数，0 关闭结果缓存，仍合并正在进行的相同请求 |
| `VIDEO_PARSE_CONCURRENCY` | 2 | 每个 API 进程的元数据解析子进程数，允许 1–4 |

10Mbps 理论约 1.25MB/s；通常指公网出站，以云厂商规格为准。音频上传与用户下载会竞争出站带宽，因此先保持 ASR 单路。以上并发值不是跨进程的分布式限额：本轮只支持单个 Solo Worker，不要通过增加 Worker 数量扩大并发。

## 实现和边界

- 分段按输入顺序合并，进度只递增；失败、取消或超时会取消并等待兄弟请求退出，再清理临时音频目录。
- LLM 在发起请求前预留输出 Token，完成后按实际用量释放余量。请求独立携带上限，不修改共享模型实例。缺失用量、取消或网络错误按预留额度保守计入本次操作预算，用户权益结算仍走原有 TaskService / Repository。
- API 的解析、下载创建、转写、总结、音频提取和权益入口共享同一个事件循环内的元数据缓存。最多 64 条、JSON 数据合计 8MiB；最多 32 个不同 URL 解析在途，超过返回 `PARSE_BUSY`。每个请求包括 DNS 校验和等待解析槽位的预算为 35 秒，终止子进程的清理时间另计。缓存不跨进程、不持久化，不缓存失败或 DNS 安全决定。
- 每次缓存访问前仍校验 URL 和 DNS。实际下载、字幕提取继续由现有适配器重新访问平台、验证视频身份及格式；缓存中的格式不会绕过下载校验。
- 所有等待者退出后停止共享解析子进程；一个等待者取消不会中断其他用户等待的同一次解析。
- 保留 500ms 进程 Watchdog、任务总超时、文件体积保护、5GB 存储配额、清理和终态 CAS。

## 启动和回退

从项目根目录进入后端，按照 `.env.example` 或 `.env.docker.example` 准备私有环境文件，并启动 PostgreSQL/Redis。生产使用 `deploy/production.env.example` 的新配置，不覆盖既有密钥。

```bash
cd backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另一个终端：

```bash
cd backend
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=INFO
```

生产部署沿用已有 systemd unit，在正常发布后重启 API 与 Worker 加载配置。此轮没有自动重启开发进程或连接生产服务器。

回退到逐段执行：设置 `LLM_CHUNK_CONCURRENCY=1` 和 `ASR_CHUNK_CONCURRENCY=1`。关闭元数据结果缓存：设置 `VIDEO_PARSE_CACHE_TTL_SECONDS=0`，重启 API。数据库结构无变更。

## 验证和测量

2026-10-31 本地验收结果：`uv run pytest` 全量 **360 passed**（22.60 秒），其中新增性能与并发测试 **19 项**。覆盖分段有序合并、并发上限、失败/取消/超时后的兄弟请求清理、Token 预留、解析缓存容量与过期、DNS 重校验、共享解析取消竞态，以及阶段日志。

另以临时 API 进程在 `127.0.0.1:18081` 验证，`/api/v1/health/live`、`/api/v1/health/ready` 和 `/docs` 均返回 HTTP 200，ready 中 PostgreSQL/Redis 均为 ready。`git diff --check` 通过。临时验证服务已退出；未调用付费模型或进行生产服务器压测。

```bash
uv run pytest
uv run pytest tests/test_performance.py -q
curl -sS http://127.0.0.1:8000/api/v1/health/live
curl -sS http://127.0.0.1:8000/api/v1/health/ready
```

打开 `http://127.0.0.1:8000/docs`，用开发或测试账号授权。对同一个公开视频重复调用 `POST /api/v1/videos/parse`，验证响应一致并在 API 日志观察 `metadata_cache outcome=miss`、`hit`；同时请求时可出现 `shared`。等待 TTL 过期后应重新解析。使用正常任务创建/权益确认流程处理长视频，订阅任务 SSE，检查进度、取消和最终文稿时间戳。

日志形如：

```text
performance stage=queue task_id=<uuid> duration_ms=1200 outcome=started
performance stage=audio_acquire task_id=<uuid> duration_ms=8000 outcome=ok
performance stage=asr task_id=<uuid> duration_ms=20000 outcome=ok
performance stage=summary task_id=<uuid> duration_ms=16000 outcome=ok
```

还包含 `metadata_parse`、`subtitles`、`transcript_acquire`、`llm_map`、`llm_reduce`、`mindmap`、`download`、`question`。日志不含 URL、文稿、提示词、密钥或错误详情。父阶段和子阶段重叠，不能相加：例如 `summary` 包含 `llm_map` 和 `llm_reduce`；既有 AI usage 的 `duration_ms` 是请求时长之和，不是并行后的墙钟耗时。`queue` 包含权益等待在内的创建至启动时间。

服务器实测应区分冷缓存和热缓存，并固定视频、模型与参数。比较并发 1 / 2 的各阶段 p50/p95、总耗时、错误率、峰值内存及出站带宽。用独立测试数据或受控版本区分结果缓存；不要删除生产结果或直接用缓存命中时长宣称模型提速。测试用 mock 验证并发和取消，不会调用付费模型，也不代表真实服务器提速比例。

## 后续队列拆分需要的改造

当前媒体处理持有全局存储租约，抢锁失败返回 `STORAGE_BUSY`；AI Celery 任务内也包含获取文稿。不能直接开多个 Worker 或仅按任务名称分队列。

下一轮需将文稿准备与模型生成拆成可恢复阶段，使用数据库条件更新衔接，保持取消、重试与权益只结算一次；将全程存储租约替换为配额预留、提交短锁及崩溃回收；模型调用增加跨 Worker 并发限额，随后再启用媒体/AI 双队列。对象存储分发根据实际成品下载流量决定。本轮不修改任务状态结构或上线多 Worker。
