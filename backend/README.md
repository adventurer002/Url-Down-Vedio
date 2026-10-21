# VideoHub AI backend

Python 3.14，依赖通过本目录的 `pyproject.toml` / `uv.lock` 管理。
完整配置、启动、Celery 验证与接口说明见 [根目录 README](../README.md)。

## 阶段 2：仅解析视频元数据

在 `backend/` 启动：

```sh
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

请求只接受 `url`；不接受代理、Cookie 或其他 yt-dlp 参数：

```sh
curl --max-time 40 -X POST http://127.0.0.1:8000/api/v1/videos/parse \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=BaW_jenozKc"}'

curl -i --max-time 5 -X POST http://127.0.0.1:8000/api/v1/videos/parse \
  -H 'Content-Type: application/json' -d '{"url":"http://127.0.0.1/private"}'
```

第一条须在网络可达且视频仍公开可用时验证；平台可用性以实测为准。
第二条预期 HTTP 400、`error.code=SSRF_BLOCKED`。
Swagger `/docs` 的 videos / parse 可直接执行；PG、Redis 和 Celery 无须运行即可解析。

成功响应 `data` 包括平台、平台视频 ID、标题、时长（秒）、封面、上传者、字幕标记、
字幕语言列表（含自动字幕）与 `download_options`。原始流地址不返回。
平台身份来自 extractor 和视频 ID，URL 片段与已知追踪参数不参与身份。
仅列出源中实际存在、可组成音视频的 1080p / 720p / 480p 档位，以及有音轨时的 MP3 档位。
缺失分辨率不补齐、不向上缩放；纯视频须有配套音轨。DRM 流被过滤。
MP4 后续按需合并或转码；本阶段的选项仅描述能力，尚未实现下载或转码。

`estimated_size_bytes` 为估算值：优先使用源文件大小，分离流将音视频大小相加，
其次使用码率 × 时长；需转码或无法估算时返回 null。
MP3 暂按 192 kbps 目标码率估算；容器开销与后续实际产物可能存在差异。

安全与执行边界：

- 仅支持 HTTP/HTTPS 公网地址。检查全部 A/AAAA 结果，任一非公网地址即拒绝。
- 输入 DNS 校验限时 2 秒，不重试；子进程解析总时限 30 秒，单次 socket 超时 15 秒。
  yt-dlp 网络/提取失败最多重试 1 次，总时限优先；退出清理最多额外约 2.5 秒。
- yt-dlp 使用独立进程组，超时、请求取消或结果超过 8 MiB 时终止整个组。
  初始请求、重定向和实际连接的 DNS 结果均校验，禁用其他网络处理器、泛用媒体探测、
  JS 子进程及远程组件，不读取 Cookie/netrc，不写媒体、字幕、封面、info JSON 或磁盘缓存。
- 可选代理仅从 Worker 子进程继承的服务端环境变量 `YTDLP_PROXY` 读取。
  例如先 `export YTDLP_PROXY=http://127.0.0.1:7890` 再启动 API；不读取用户请求参数。
  未设置时显式禁用系统代理。代理是可信服务端设施，若代理自行解析目标 DNS，
  代理也必须禁止访问私网并防止 DNS 重绑定；应用无法约束远端代理的实际路由。
- 播放列表、直播和需登录资源返回 `UNSUPPORTED_URL`，无任何登录或 Cookie 接口。
  解析超时为 `PARSE_TIMEOUT` / 504，其他提取错误为 `PARSE_FAILED` / 502；
  错误不包含 yt-dlp 原始日志、代理地址或凭据。

验证：`uv run pytest`。固定样本测试不请求平台或下载媒体；包含真实子进程超时终止测试。

## 阶段 3：数据库、统一任务与 SSE

先按根目录 README 启动 PostgreSQL 和 Redis。在 `backend/` 执行：

```sh
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

另开终端进入 `backend/`，启动单并发 Worker：

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

第三个终端进入 `backend/`，通过内部命令调用 `TaskService.create` 创建并入队演示任务。
它只模拟约 3 秒的进度，不解析 URL、不下载或生成媒体：

```sh
TASK_ID=$(uv run python -m app.modules.tasks.worker)
curl -N --max-time 15 "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/events"
curl "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID"
curl -X POST "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/cancel"
```

第一条 curl 会依次输出 `event: progress`，每帧含 `id: <seq>` 和 JSON 数据，终态自动断开。
最后一条在已完成任务上返回当前 `completed` 状态，不改变结果或取消标记。
验证运行中取消可另建任务后立即调用 cancel，再订阅 events：

```sh
TASK_ID=$(uv run python -m app.modules.tasks.worker)
curl -X POST "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/cancel"
curl -N --max-time 15 "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/events"
```

如任务仍排队，直接返回 `cancelled`；已运行时先返回 `cancel_requested=true`，
Worker 每约 500ms 检查取消标记并提交 `cancelled`，SSE 收到终态后关闭。
Swagger `/docs` 中 tasks 分组支持查询和取消；实时流建议用 `curl -N` 验证。
任务创建只提供内部 Python 契约和演示命令，本阶段没有公开的任务创建 HTTP 接口。

实现约定：

- `videos` 由 video 模块持有，平台与视频 ID 联合唯一；`tasks` 由 tasks 模块持有；
  `media_files` 由 media 模块持有。本阶段只建媒体表，尚未实现媒体下载或文件写入。
  内部系统演示任务允许 `video_id=null`；关联视频的任务仍受外键约束。
- PostgreSQL 使用原生 UUID、JSONB、任务状态枚举及进度范围约束。
  只在创建、状态跃迁、取消请求和最终结果时写任务记录；高频进度只写 Redis。
- Redis Lua 原子完成节流、自增 `seq`、Hash 快照和 Pub/Sub 发布。
  同状态至少间隔 500ms；状态变化立即发布；已发布终态不能被迟到事件覆盖。
  快照 `task:snapshot:{id}` 与取消标记保留 24 小时。
  独立 `task:seq:{id}` 计数器不随快照过期，避免序号重置；任务永久删除时也应清理计数器。
- SSE 等待订阅 ACK 后才读取 PG/Redis 首帧，再过滤 `seq <= 已发送序号` 的消息。
  空闲时每秒检查持久化状态，补偿提交 PG 后未能发布的终态；每 15 秒发送心跳注释。
  单连接最多 300 秒，客户端重连会收到最新快照，不提供历史事件重放。
- 取消使用数据库 `UPDATE ... WHERE status ...`，完成也检查 `cancel_requested=false`。
  PG 是状态裁决来源，Redis 取消标记失效或不可用时 Worker 回查 PG，不使用 revoke。
  取消时先提交 PG 再写 Redis；Redis 故障可能返回 503，但已经提交的取消意图不会丢失。
- Redis 不可用时 GET 降级返回 PG 状态与 `seq=0`，此时进度可能停在上次状态跃迁值。
  SSE 建连前依赖不可用返回统一 503；流建立后故障发送含统一错误结构的 `event: error` 并关闭。
- Redis 单次操作总时限 2 秒；数据库调用有连接/SQL 超时与 3 秒调用预算；
  演示 Worker 总执行预算 15 秒。入队失败会记录为 `failed`，不自动无限重试。
  当前未实现事务 outbox 或 Worker 崩溃后的任务自动恢复。
- `expires_at` 为后续过期回收提供字段；本阶段尚无定时过期扫描器。

验证迁移与测试：

```sh
uv run alembic current
uv run alembic check
uv run pytest
```

测试使用临时 SQLite 验证 Repository/CAS，另启动隔离 Redis Unix socket 测试真实 Lua、节流和 Pub/Sub，
不会清空开发 Redis。需可执行的 `redis-server`；也可提供专用测试实例 `TEST_REDIS_URL`。
没有二者时，3 项 Redis 集成测试会跳过，其余测试继续执行。

## 阶段 4：下载、Watchdog 与受控文件

先安装服务端可执行文件（Python 依赖继续由 uv 管理）：

```sh
brew install ffmpeg
uv sync --locked
uv run alembic upgrade head
```

本阶段复用 `0001` 的视频、任务、媒体表，无新增表结构。
在根目录 `.env` 可按需设置以下服务端配置；默认值如下，宿主机与容器环境含义相同：

```dotenv
DOWNLOAD_TIMEOUT_SECONDS=600
MAX_FILE_SIZE_MB=512
STORAGE_MAX_QUOTA_MB=5120
STORAGE_MIN_FREE_MB=256
STORAGE_FILE_TTL_HOURS=24
```

`MAX_FILE_SIZE_MB` 限制整个任务临时目录的总大小，包含源音视频、分片与转换输出；
合流时同时保留源文件和最终文件，预算需留足空间。下载前会为最大任务预算检查总配额和剩余磁盘。
本地存储通过非阻塞文件锁串行化下载与清理；其他 Worker 正在使用存储时返回 `STORAGE_BUSY`，不无限等待。

终端一，在 `backend/`：

```sh
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

终端二，在 `backend/`（PG / Redis 已启动）：

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

终端三，先解析真实公开视频并查看实际档位；示例链接是否可用受平台和网络影响：

```sh
curl --max-time 40 -X POST http://127.0.0.1:8000/api/v1/videos/parse \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=BaW_jenozKc"}'
```

从返回的 `download_options` 选择真实存在的 `preset_id`，再提交下载。
下方 `video_480_mp4` 仅为示例，若视频没有该档位，需替换为实际档位：

```sh
curl --max-time 45 -X POST http://127.0.0.1:8000/api/v1/downloads \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=BaW_jenozKc","preset_id":"video_480_mp4"}'
```

预期 HTTP 202，`data.task_id` 为数据库任务 ID。将其填入下面变量：

```sh
TASK_ID='将返回的任务 UUID 填在这里'
curl -N --max-time 620 "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/events"
curl "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID"
```

成功终态 `data.result_payload` 包含 `file_id`、相对 `download_url`、安全文件名、大小及过期时间。
文件下载接口只接收 UUID，不接受磁盘路径或 storage_key：

```sh
FILE_ID='将 result_payload.file_id 填在这里'
curl --fail --max-time 120 -D /tmp/videohub-download.headers \
  -o /tmp/videohub-download.mp4 "http://127.0.0.1:8000/api/v1/files/$FILE_ID/download"
```

MP3 档位请将本地输出文件名改为 `.mp3`。接口使用 `FileResponse`，支持范围请求；
不存在、已过期、物理文件缺失或不安全存储路径均返回统一 `FILE_NOT_FOUND` / 404。

验证中途取消：重新提交一个较长、仍公开可用的视频，在一个终端订阅 SSE，
另一个终端看到 `running` 或 `processing` 后执行：

```sh
curl -X POST "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/cancel"
```

预期先返回取消意图，随后 SSE 收到 `cancelled`，错误码为 `TASK_CANCELLED`。
如果资源很小、任务已完成，取消返回当前 `completed`，不会删除完成文件。
也可在 Worker 尚未启动时提交并取消，验证排队任务直接进入 `cancelled`。

实现与故障验证：

- 请求仅接受 `url` 与固定档位；输入 URL、提取器后续请求、重定向与实际连接均经过原有 SSRF 校验。
  Worker 重新提取元数据并比对平台视频身份；服务端筛选精确高度和音轨，然后对每个已选源使用固定 `best` 选择器。
  不使用请求提供的 yt-dlp 参数、代理或 Cookie。可选 `YTDLP_PROXY` 仍只来自服务端环境。
- 原生 HTTP/HLS/DASH 传输均经受保护的 yt-dlp 网络层；需要外部网络下载器的流被拒绝。
  HLS 内部回退到联网 FFmpeg 的路径也被拦截。FFmpeg 仅处理任务目录内本地输入，协议白名单为 `file,pipe,crypto`。
- 兼容 H.264/AAC 时优先流拷贝 MP4；其他编码转换为 H.264/AAC，单线程；MP3 为 192 kbps。
  输出路径固定，成功后移动为 `storage/files/{file_id}.mp4` 或 `.mp3`。
- Watchdog 每 500ms 非阻塞轮询进程、取消、总超时和临时目录总大小，同时检查配额与可用磁盘。
  控制通道单次预算 3 秒。取消/超时/超体积时向整个进程组发送 SIGTERM，最多等 3 秒，再升级 SIGKILL。
  确认进程组退出后删除任务临时目录，再由任务 Repository CAS 写入终态。
  无法确认进程树退出时记录 `PROCESS_CLEANUP_FAILED`，保留临时目录并避免清理活进程的数据。
- 最终文件先移动，媒体记录与任务完成 CAS 在同一个数据库事务中提交。
  取消赢得竞争时回滚注册并删除产物；注册失败也回滚完成状态并清理文件。
  若数据库提交结果暂时无法确定，保留可能已注册的产物，避免误删完成文件，后续清理可回收无记录的旧文件。
- `DOWNLOAD_TIMEOUT`、`FILE_TOO_LARGE`、`STORAGE_QUOTA_EXCEEDED`、`INSUFFICIENT_DISK_SPACE` 等通过任务错误字段与 SSE 返回。
  可临时缩短服务端 `DOWNLOAD_TIMEOUT_SECONDS`，或降低 `MAX_FILE_SIZE_MB` 后重启 Worker 验证失败分支。

手动运行过期清理（默认也在每次下载前运行一次）：

```sh
uv run python -m app.infrastructure.storage.cleanup
```

清理过期媒体记录和物理文件、终态残留目录，以及超过保留时间且无记录的孤儿文件/目录。
活进程目录不会删除；无活进程且已过期的遗留任务会通过任务 Repository 标记 `expired`。
尚未配置定时调度，长期不发生下载时可按需运行此命令。

```sh
uv run pytest
```

下载测试覆盖 202 入队、视频去重、真实父子进程的取消/超时/体积终止、文件分发、过期清理、
完成 CAS 与媒体注册回滚，以及真实 yt-dlp HTTP 传输和 FFmpeg 合流。
媒体测试使用临时生成的短片和隔离测试服务器，提取器/DNS 由测试夹具代替；生产 SSRF 校验不放宽。
真实媒体测试需要 `ffmpeg`、`ffprobe`，缺少时会跳过这一项。
