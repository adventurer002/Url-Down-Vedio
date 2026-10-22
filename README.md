# VideoHub AI · 本地 V1

React 19 黑白视频工作台 + FastAPI / PostgreSQL / Redis / Celery Solo 后端。
支持公开视频解析与下载、按需纯音频、字幕优先转写、结构化总结、JSON 思维导图、任务历史与安全过期清理。
默认不安装 PyTorch 或本地大模型，适配 M1 8GB / 256GB。

## 一键启动完整应用

需要 Python 3.14、uv、Node.js 20.19+ 或 22.12+、pnpm、FFmpeg。
macOS 可用 Homebrew 安装 PostgreSQL 16 / Redis / FFmpeg；Python 依赖只由 `backend/uv.lock` 管理，前端由 `frontend/pnpm-lock.yaml` 管理。

```sh
# PostgreSQL / Redis 已在本机运行
./scripts/dev.sh

# 或：由 Docker Compose 提供 PostgreSQL / Redis，应用仍在宿主机运行
./scripts/dev.sh --docker

# 后续启动，依赖没有变化时可跳过安装
./scripts/dev.sh --skip-install
```

不要同时执行上述三条，也不要同时启动占用相同端口的 Homebrew 与 Compose 数据服务。
脚本会在根目录 `.env` 不存在时复制 `.env.example`，随后安装锁定依赖、执行 Alembic、
启动 **API + 必需的 Celery Solo Worker + Vite**，等待依赖就绪和 Worker 的 ready 日志后打印入口：

- 工作台：<http://127.0.0.1:5173>
- Swagger：<http://127.0.0.1:8000/docs>
- 当前运行日志：`.runtime/api.log`、`worker.log`、`frontend.log`（不纳入 Git）。

脚本不会杀死已有服务；8000 / 5173 端口被占用时会明确退出，请先停止之前的启动终端。
只运行一个 Solo Worker，避免在 8GB 设备上重复启动下载进程。
Ctrl+C 关闭应用，Worker 使用 warm shutdown，让当前任务受 Watchdog 保护完成后退出；
不要用强杀 Worker 的方式取消下载，应在界面点击“取消任务”。等待上限 930 秒，超限保留 Worker 并报告 PID，
避免杀死 Watchdog 后留下独立下载进程。Compose 数据服务保持运行，可另行 `docker compose stop`。

已有宿主机数据库但尚未初始化用户时，仅首次运行：

```sh
brew services start postgresql@16
brew services start redis
psql postgres -c "CREATE USER videohub_user WITH PASSWORD 'videohub_pass';"
createdb -O videohub_user videohub_dev
```

默认开发数据库：`videohub_dev`，用户 / 密码 `videohub_user` / `videohub_pass`。
配置优先级是进程环境 > 根目录 `.env` > 代码默认值；不要把已有生产凭据放进模板。

## 环境配置与运行边界

`.env.example` 使用 `localhost`，适用于 Homebrew 和“Compose 依赖 + 宿主机应用”。
`.env.docker.example` 使用 `postgres` / `redis`，仅供应用进程位于同一容器网络时使用，
不要直接复制给宿主机应用。当前 Compose 只提供 PostgreSQL 16 与 Redis 7，设置内存上限、健康检查、环回端口及日志轮转。

前端 `VITE_API_BASE_URL` 默认 `http://localhost:8000/api/v1`，构建时注入，只能存放公开配置。
更换来源时同步设置后端 `CORS_ORIGINS` JSON 数组，默认允许 localhost / 127.0.0.1 的 5173 端口。
本地 V1 没有账户隔离，任务历史属于当前本地实例，默认仅绑定环回地址。

| 配置 | 默认值 / 用途 |
| --- | --- |
| `STORAGE_LOCAL_PATH` | `./storage`，按项目根目录解析 |
| `STORAGE_MAX_QUOTA_MB` | `5120`，统计文件与临时目录的总占用 |
| `STORAGE_MIN_FREE_MB` | `256`，保留的磁盘可用空间 |
| `MAX_FILE_SIZE_MB` | `512`，单任务临时文件上限 |
| `DOWNLOAD_TIMEOUT_SECONDS` | `600`，下载 / 合流总时限 |
| `STORAGE_FILE_TTL_HOURS` | `24`，媒体保留与孤立临时文件清理期限 |
| `ASR_PROVIDER` | `auto`，有云 Key 才启用云 ASR；可显式 `mock` 离线验证 |
| `ASR_API_BASE_URL` / `ASR_API_KEY` / `ASR_MODEL` | 服务端 ASR 配置，不对前端暴露 |
| `LLM_PROVIDER` | `auto`，无 Key 自动采用确定性 `heuristic-v1` |
| `LLM_API_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 服务端模型配置 |
| `LLM_CHUNK_CHARS` / `LLM_MAX_CHUNKS` | `12000` / `64`，长文稿 Map-Reduce 预算 |
| `AI_TASK_TIMEOUT_SECONDS` | `900`，包含字幕 / ASR / LLM 的 AI 任务总时限 |

使用阿里云百炼千问语音识别时，在根目录 `.env` 配置：

```dotenv
ASR_PROVIDER=qwen
ASR_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ASR_API_KEY=你的百炼密钥
ASR_MODEL=qwen3-asr-flash
ASR_TOTAL_TIMEOUT_SECONDS=180
```

模型与密钥须属于对应地域。修改后停止原启动终端，再运行 `./scripts/dev.sh --skip-install`，让 API 和 Solo Worker 都加载新配置。
适配器通过 Watchdog 将音频转换为单声道 16kHz WAV，每 120 秒分段请求并及时清理切片。
时间轴表示音频分段范围，不是逐句对齐时间戳。模型错误返回 `ASR_MODEL_UNSUPPORTED`，权限错误返回 `ASR_AUTH_ERROR`。
转写成功后，在工作台生成总结或思维导图会复用文稿缓存。

没有字幕且未配置 ASR 时，任务明确返回 `ASR_NOT_CONFIGURED`，不会伪造识别结果。
仅离线验收可显式使用 `ASR_PROVIDER=mock`；其文稿不代表真实识别。Heuristic 总结是摘录，不执行翻译或生成式推理。
云端接口经过 HTTP 契约测试，真实云服务仍需自行配置 Key、模型和额度；本次不使用付费 API 验收。
公开平台能力也取决于网络、资源可用性与登录限制；不提供用户 Cookie / 代理上传入口。

## 接口与手动验证

所有成功响应为 `{"data": ...}`，错误为 `{"error":{"code","message","request_id"}}`。
API、PG、Redis 绿点表示这三者就绪；不代替 Worker 就绪，一键脚本会另行确认 Worker 已启动。

| 方法与路径（`/api/v1`） | 作用 |
| --- | --- |
| `GET /health/live`、`GET /health/ready` | 存活 / 短超时依赖检查 |
| `POST /videos/parse` | 返回当前资源真实可用的业务下载档位 |
| `POST /downloads`、`POST /audio/extractions` | 创建下载 / 按需音频任务，202 |
| `POST /transcriptions` | 字幕优先，缺失时按需音轨与 ASR |
| `POST /ai/summaries`、`POST /ai/mindmaps` | 自动衔接转写，创建 AI 任务 |
| `GET /tasks?limit=20&offset=0` | 服务器任务历史，limit 1–100、offset ≥ 0 |
| `GET /tasks/{id}`、`GET /tasks/{id}/events` | 完整任务记录 / SSE 进度 |
| `POST /tasks/{id}/cancel` | CAS 协作取消，禁止覆盖已完成任务 |
| `GET /files/{id}/download` | 未过期文件的受控下载 / Range |
| `GET /transcriptions/{id}`、`GET /ai/results/{id}` | 文稿 / JSON 与 Markdown、用量元数据 |
| `POST /system/cleanup` | 清理过期媒体、终态临时目录与过期孤立文件，返回释放字节数 |

历史响应为 `data.items/total/limit/offset`，含视频信息、任务类型、状态、Redis 最新匹配快照进度及结果 ID。
按 `created_at DESC, id DESC` 稳定排序。PG 状态优先，Redis 故障时仍可查询历史。
只有记录未过期、物理文件存在且大小匹配时才提供下载入口；AI / 文稿结果 ID 保留。
抽屉打开时读取服务器记录，运行中任务继续 SSE / 轮询；本地没有视频缓存也能跳回工作台，按需重新解析下载档位。

```sh
curl --max-time 5 http://127.0.0.1:8000/api/v1/health/ready
curl --max-time 6 'http://127.0.0.1:8000/api/v1/tasks?limit=20&offset=0'
curl --max-time 45 -X POST http://127.0.0.1:8000/api/v1/videos/parse \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.bilibili.com/video/BV1g2JX6nESP"}'

# 将 preset_id 换成解析接口实际返回的档位
curl --max-time 45 -X POST http://127.0.0.1:8000/api/v1/downloads \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.bilibili.com/video/BV1g2JX6nESP","preset_id":"video_480_mp4"}'

TASK_ID='替换为返回的任务ID'
curl -N --max-time 310 "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/events"
curl --max-time 5 "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID"
curl --max-time 5 -X POST "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/cancel"

curl --max-time 45 -X POST http://127.0.0.1:8000/api/v1/system/cleanup
```

清理也可从历史抽屉底部执行，或运行内部命令：

```sh
cd backend
uv run python -m app.infrastructure.storage.cleanup
```

返回 `files`、`temporary_directories`、`orphan_files`、`freed_bytes`、`used_bytes`。
释放字节数按清理前后的逻辑文件长度统计；不存在的文件不重复计数。
清理不删除有效媒体、文稿或 AI 缓存；进程组仍存在时保留临时目录。
存储 lease 与下载互斥；下载占用期间可能返回 `STORAGE_BUSY`，稍后重试。
配额超限返回 507 / `STORAGE_QUOTA_EXCEEDED`，可用空间不足为 `INSUFFICIENT_DISK_SPACE`。

API、Worker、HTTP 客户端和访问日志在输出前脱敏 URL 中的
`token/access_token/key/api_key/auth/sig/signature/cookie/sessdata` 等值，替换为 `***REDACTED***`。
覆盖大小写、编码参数、重复参数、延迟格式化、异常堆栈；保留普通参数。日志仍不应作为密钥存储。

## 自动化回归

```sh
cd backend
uv run alembic upgrade head
uv run alembic check
uv run pytest

cd ../frontend
pnpm run type-check
pnpm test:unit
pnpm run build

cd ..
docker compose config --quiet
```

测试使用临时数据库、Redis / HTTP 服务和固定媒体样本，不依赖付费模型或第三方视频持续在线。
额外联调记录：当前本地实例已成功下载 B 站 BV1g2JX6nESP 的 480p 文件，并验证 Range 分发。
云端 ASR / LLM 的真实效果与额度不在离线自动化通过的结论范围内。

React 迁移回归（2026-10-22）：后端 **193 项通过**，前端 **32 项通过**；`tsc --noEmit`、Vite 构建和 Compose 配置校验通过。
一键脚本已验证启动、正常退出 / 再启动及端口冲突时拒绝重复启动。

## 21 项 V1 验收清单

下表“通过”指列出的自动化或本地联调证据；云端实测边界单独注明，不把 Mock 当作真实识别。

| # | 核对项 | 当前证据 / 核对结果 |
| --- | --- | --- |
| 1 | [x] 一键启动 API、PG、Redis、Solo Worker、前端 | `scripts/dev.sh --skip-install` 实测就绪，包含迁移与 Worker ready 检查 |
| 2 | [x] 双健康检查、依赖故障与 1.5s 就绪上限 | `test_health.py` |
| 3 | [x] 统一错误、404/422 与 Request ID | `test_health.py`、`test_history_and_security.py` |
| 4 | [x] HTTP(S)、IPv4/IPv6 / DNS SSRF 拦截 | `test_video_parse.py` |
| 5 | [x] 平台身份、元数据与动态清晰度档位 | `test_video_parse.py`；B 站真实解析 |
| 6 | [x] 缺失档位过滤、流组合与安全选择器 | `test_video_parse.py`、`test_download.py` |
| 7 | [x] TaskService 统一入队与 202 响应 | 下载 / 音频 / 文稿 / AI 测试 |
| 8 | [x] 视频下载与 FFmpeg 合流 / 转码 | 固定媒体真实进程测试；B 站真实下载 |
| 9 | [x] 文件 Range、文件名安全、过期/不存在 404 | `test_download.py`；真实文件 206 |
| 10 | [x] queued / running 取消、终态 CAS 与进程组退出 | `test_tasks.py`、`test_download.py` |
| 11 | [x] 总超时、单任务体积限制与临时清理 | `test_download.py` |
| 12 | [x] 总配额统计 tmp+files 并拒绝超限 | `test_history_and_security.py` |
| 13 | [x] 过期文件、孤立文件清理与释放字节统计 | `test_history_and_security.py`；真实清理接口保留有效文件 |
| 14 | [x] 字幕优先与 VTT / SRT / JSON3 清洗 | `test_media_and_transcript.py` |
| 15 | [x] 本地音视频复用与网络独立音轨限制 | `test_media_and_transcript.py` |
| 16 | [x] ASR 降级、SHA256 与多维转写缓存 | Mock / HTTP 契约与缓存测试；千问 qwen3-asr-flash 已实测 502 秒音频分段转写完成 |
| 17 | [x] LLM 适配与长文稿 Map-Reduce / Schema 修复 | `test_ai_module.py`；真实云 LLM 待服务端 Key 配置 |
| 18 | [x] AI 缓存、JSON 树与 Markdown / SVG 导出 | 后端缓存测试与前端浏览器导出验收 |
| 19 | [x] 服务器任务历史、分页、关联视频与有效结果 | 专项后端 / 前端测试与抽屉联调 |
| 20 | [x] URL 敏感参数日志脱敏 | 专项测试及真实 Uvicorn 访问日志探针 |
| 21 | [x] SSE 去重/轮询回退、移动 Tab、类型与构建 | Vitest、tsc、Vite build、浏览器响应式验收 |

更多实现与配置细节：[后端文档](backend/README.md) · [前端文档](frontend/README.md)。

## 阶段 8 文件清单

- 后端：`app/core/logging.py`、`app/__init__.py`、API / Celery 日志初始化；`tasks/history.py`、
  任务 Repository / Service / Router；视频元数据与媒资有效性查询契约；存储清理字节统计；
  `modules/system/router.py`；`tests/test_history_and_security.py`。
- 前端：历史 / 清理 API 类型与客户端、服务器历史合并、任务抽屉、历史工作台档位刷新与对应样式 / 测试。
- 本地运行：`scripts/dev.sh`、`scripts/dev.py`、`compose.yaml`、`.gitignore` 与根目录 / 前端 README。


## React 迁移（2026-10-22）

前端已从 Vue 3 / Pinia 迁移至 React 19 / React Router，采用 Context + `useSyncExternalStore` 管理共享状态。
保留原有页面、响应式样式、API 契约、任务 SSE / 轮询机制及本地视频 / 任务缓存；后端无代码变动。
现有启动脚本和部署方式继续适用。登录注册、充值和后台管理属于后续功能。

迁移文件清单、测试与浏览器验收边界见 [前端文档的 React 迁移说明](frontend/README.md#react-迁移说明2026-10-22)。
