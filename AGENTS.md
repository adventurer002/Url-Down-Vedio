# VideoHub AI (V1.2 Backend-First) — Project Architecture & Coding Rules (AGENTS.md)

## 1. 开发策略：纯后端先行（Backend-First）
- **阶段 1–6 仅开发 `backend/`**：严禁在阶段 1–6 创建或修改 `frontend/` 目录。所有接口通过 FastAPI Swagger UI (`/docs`)、`pytest` 及 `curl` 验证。
- **工具链约定**：Python 后端统一在 `backend/` 目录下使用 `uv` 管理虚拟环境与依赖（`uv add`、`uv run`），维护 `pyproject.toml` 与 `uv.lock`。

## 2. 六大核心架构铁律
1. **模块契约隔离**：各业务模块（`video`、`tasks`、`media`、`ai`）不得直接修改其他模块的数据模型，必须通过明确的 `Service` 或 `Repository` 契约交互。
2. **外部 I/O 强超时与重试**：所有外部 I/O（`yt-dlp`、`FFmpeg`、ASR、LLM API、网络请求）必须显式定义连接超时、总超时（Timeout）、失败处理及可控重试策略，严禁无限期阻塞。
3. **统一异步任务编排**：下载、音频提取、转写和 AI 处理均通过统一 `TaskService` 创建并交由 Celery Worker 执行，严禁在 FastAPI HTTP 请求进程中直接执行耗时任务。
4. **第三方能力适配器化**：所有第三方能力必须经过 `typing.Protocol` 适配器接口封装，禁止在 `router.py` 或业务逻辑中散落 `yt-dlp`、`FFmpeg` 或某家 LLM SDK 的直接调用。
5. **协作式取消 + 进程级 Watchdog 兜底（适配 Celery Solo 模式）**：
   - 本地开发在 M1 8GB 上使用 `--pool=solo -c 1` 运行 Celery Worker。鉴于 Solo 池在执行任务期间会阻塞 Celery 远程控制指令且软硬超时信号受限，**严禁依赖 `celery revoke(terminate=True)` 或单纯依赖 Celery 自带超时来终止外部任务**。
   - **标准执行与取消机制**：
     1. 用户点击取消时，API 将取消请求标记写入 Redis 与数据库。
     2. Worker 启动 `yt-dlp` 或 `FFmpeg` 等子进程时，必须使用独立进程组（`start_new_session=True`）及非阻塞监控循环（Watchdog Loop，每 500ms 轮询一次）。
     3. 监控循环持续检查三项条件：**是否收到取消标记**、**是否超过任务总超时（Timeout）**、**是否超出最大文件体积限制**。
     4. 一旦触发任一中止条件，Worker 立即向整个子进程组发送 `SIGTERM`（超时未退出则升级为 `SIGKILL`），确认子进程树完全退出后清理 `storage/tmp/{task_id}` 临时目录，并通过数据库条件更新（CAS）持久化最终状态（防止“刚完成瞬间点击取消”的竞态覆盖）。
6. **多级缓存键与数据隔离**：
   - **视频身份**：`(platform, platform_video_id)` 联合唯一索引（剥离 URL 追踪参数）。
   - **字幕/转写缓存键**：`video_id + source_type (subtitle/asr) + language + content_version + asr_model`。
   - **AI 总结缓存键**：`transcript_content_hash + llm_model + prompt_version + output_language`。
   - **思维导图缓存键**：`source_content_hash + schema_version + llm_model`。

## 3. 关键数据流转与结构规范
- **统一响应与错误结构**：
  - 所有接口挂载于 `/api/v1`。
  - 成功响应：`{"data": ...}`。
  - 错误响应：`{"error": {"code": "UPPER_SNAKE_CODE", "message": "可读错误描述", "request_id": "req_xxx"}}`。
- **视频格式档位聚合**：解析接口返回业务层预设档位（如 `video_1080_mp4`、`video_720_mp4`、`video_480_mp4`、`audio_mp3`），由后端决定流选择、容器兼容性与合并/转码策略。
- **SSE 进度推送（先订阅、后快照 + 版本号去重）**：
  - Worker 节流（每 500–1000ms）将进度与单调递增序列号 `seq` 写入 Redis Hash 快照，并通过 Redis Pub/Sub 发布事件；PostgreSQL 仅记录状态跃迁与最终结果。
  - SSE 接口在建立连接时，**必须先建立 Redis Pub/Sub 订阅，再读取一次 Redis/PG 最新快照发送首帧**，随后转发订阅到的实时事件。每个事件均携带递增 `seq`。
- **双健康检查接口**：
  - `GET /api/v1/health/live`：仅判断 FastAPI 进程存活。
  - `GET /api/v1/health/ready`：带短超时（<= 1.5s）检查 PostgreSQL 与 Redis 就绪状态，严禁泄露连接串或密码。

## 4. 安全与 M1 8GB / 256GB 硬件保护规范
- **双环境配置模板**：同时维护 `.env.example`（宿主机 Homebrew 原生开发，连接 `localhost`）与 `.env.docker.example`（Compose 容器网络，连接 `postgres` / `redis`）。
- **256GB 磁盘空间保护**：配置 `STORAGE_MAX_QUOTA_MB=5120` 与文件过期清理策略，下载前检查可用空间。
- **SSRF 防护**：严格拦截环回地址（`127.0.0.1`/`localhost`）、链路本地/云元数据地址（`169.254.169.254`）及内网私有网段（RFC1918），并对 DNS 解析后的 IP 进行二次校验。首版仅处理公开免登录资源，严禁开放用户自定义代理或上传 Cookie。
- **交付纪律**：每完成一个阶段，必须运行 `uv run pytest` 确保全绿，并输出：修改文件清单、服务启动命令、`curl`/Swagger 验证步骤、测试结果。未获指令前严禁跨阶段编码。
