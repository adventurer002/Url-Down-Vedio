# VideoHub AI backend

生产 YouTube 下载部署、服务端出口配置与验收：[YOUTUBE_PRODUCTION.md](YOUTUBE_PRODUCTION.md)。

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

## 阶段 5：按需音频与字幕优先转写

本阶段新增 `0002_transcripts_cache` 迁移，无 PyTorch、Transformers 或本地大模型依赖。
`httpcore` 原已由 httpx 安装，本阶段显式声明它，用于 ASR 网络连接的 DNS 校验与 IP 固定。
在 `backend/` 执行：

```sh
uv sync --locked
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

另一个终端进入 `backend/`：

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

音频提取只支持 MP3，HTTP 请求负责验证和入队，FFmpeg / 网络音频传输均在 Worker 执行：

```sh
curl --max-time 45 -X POST http://127.0.0.1:8000/api/v1/audio/extractions \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=BaW_jenozKc","format":"mp3"}'
```

返回 HTTP 202 与 `data.task_id/status`。沿用任务查询、SSE 与取消接口：

```sh
TASK_ID='返回的任务 UUID'
curl -N --max-time 620 "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/events"
curl "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID"
curl -X POST "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/cancel"
```

成功结果含 `file_id` 和 `/api/v1/files/{file_id}/download`，可下载 MP3。
Worker 优先复用同一视频仍有效的本地 MP3（同一 file_id，`reused=true`，保留原过期时间）；
其次从有效本地音视频文件使用 FFmpeg 抽取音轨；没有本地文件时只选择独立音频流，
使用固定 `bestaudio/best` 选择器下载已选音轨，再转为 MP3。
若平台只提供合并视频流，返回 `AUDIO_STREAM_UNAVAILABLE`，不会为提取音频下载完整高清视频。
所有 FFmpeg / yt-dlp 子进程继续使用阶段 4 Watchdog、配额、超时和协作取消。

创建转写任务：

```sh
curl --max-time 45 -X POST http://127.0.0.1:8000/api/v1/transcriptions \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=BaW_jenozKc","language":"zh"}'
```

语言可省略；显式语言支持 `zh`、`en`、`zh-Hans` 等标签并按小写规范化。
POST 返回 HTTP 202，完成后从任务 `result_payload.transcript_id` 查询：

```sh
TRANSCRIPT_ID='任务完成后返回的转写 UUID'
curl "http://127.0.0.1:8000/api/v1/transcriptions/$TRANSCRIPT_ID"
```

响应包含 `content_text`、`source_type`、`language`、`content_version`、`asr_model`、SHA256 `content_hash`
和 `segments: [{"start": 0.0, "end": 1.0, "text": "..."}]`。Swagger `/docs` 提供 audio 和 transcriptions 分组。

字幕与缓存策略：

1. Worker 先查 `(video_id, source_type, language, content_version, asr_model)` 多维缓存。
   当前版本字幕优先；ASR 缓存只匹配当前模型。版本或 ASR 模型变化会失配；未指定语言时可使用已缓存语言。
   字幕记录固定 `asr_model=none`，Mock ASR 固定 `mock-v1`，不会混入真实模型的缓存。
2. 缓存未命中时，仅通过 yt-dlp 拉目标字幕，启用手工/自动字幕并强制 `skip_download=True`。
   手工字幕优先，允许同语言地区变体；支持 VTT / SRT / JSON3，过滤其他格式。
   清洗样式、重复行与重叠滚动字幕，标准化时间轴，再计算 UTF-8 正文 SHA256。
   字幕网络失败会明确报错；仅目标语言没有可用字幕或字幕为空时才进入 ASR。
3. ASR 优先读取已有有效 MP3，其次本地视频抽音，再次按需拉独立音轨。
   转写专用音频仅放在任务临时目录，完成、失败或取消后及时删除；复用的共享媒体文件不删除。
   转写记录注册与任务完成 CAS 共用数据库事务，取消获胜时不会留下新的转写缓存。

API 请求不接受模型、API Key、Cookie、代理或 CLI 参数。
首次解析仍需平台网络可达；已记录的同一规范化 URL 可复用视频身份，避免缓存命中前再次提取元数据。

云端 ASR 配置只由服务端根目录 `.env` 或进程环境提供。例如：

```dotenv
ASR_PROVIDER=auto
ASR_API_BASE_URL=https://api.openai.com/v1
ASR_API_KEY=填写服务端密钥
ASR_MODEL=whisper-1
ASR_TOTAL_TIMEOUT_SECONDS=90
ASR_MAX_UPLOAD_MB=25
SUBTITLE_TIMEOUT_SECONDS=45
TRANSCRIPT_CONTENT_VERSION=v1
```

兼容服务可改为其 OpenAI 兼容前缀，例如 Groq 的 `https://api.groq.com/openai/v1`，
并设置服务实际支持的模型（如 `whisper-large-v3-turbo`）；服务必须支持 `verbose_json` 和分段时间轴。
适配器向 `<ASR_API_BASE_URL>/audio/transcriptions` 发送 multipart `file/model/language/response_format`
及 `timestamp_granularities[]=segment`。参考 [OpenAI Speech-to-text](https://platform.openai.com/docs/guides/speech-to-text)。
本次环境无法读取官方文档站点，字段兼容性通过 HTTP 契约测试验证，未使用真实云端凭据调用。

连接超时 5 秒、读取 60 秒、写入 30 秒，整次 ASR 操作（含至多 1 次有限重试）默认最多 90 秒。
仅对超时、网络错误及 429 / 部分 5xx 重试，401 等配置/鉴权错误不重试。
禁止跟随重定向与隐式系统代理；实际连接前校验全部 DNS 结果并连接到校验过的 IP，保留原 Host / TLS SNI。
私网 API 地址会被阻止；API Key 和底层服务响应不写入任务或对外错误。
请求上传最大默认 25 MiB，响应最大 2 MiB；暂不自动切分长音频，超限返回 `ASR_AUDIO_TOO_LARGE`。

不配置密钥时，有字幕的视频照常完成；没有字幕则返回清晰的 `ASR_NOT_CONFIGURED`，并在拉音频前停止。
不会自动把 Mock 结果伪装为真实识别结果。离线测试可显式配置：

```dotenv
ASR_PROVIDER=mock
ASR_MOCK_TEXT=离线模拟转写内容（非真实识别结果）
```

重启 Worker 后生效。Mock 只替代 ASR，不替代首次平台元数据/字幕网络获取；单元测试使用夹具避免外网。
`ASR_PROVIDER=disabled` 可明确关闭 ASR，同时保留字幕与已有缓存能力。

验证命令：

```sh
uv run alembic current
uv run alembic check
uv run pytest
```

阶段 5 测试覆盖本地视频/音频复用、独立音轨限制、VTT/SRT/JSON3 清洗、缓存维度、字幕优先与 ASR 回退、
SHA256、HTTP multipart/超时/有限重试、DNS 固定、取消 CAS 和临时音频清理。
字幕集成测试使用隔离 HTTP 服务，实际验证只请求字幕而不下载媒体。

## 阶段 6：结构化总结与 JSON 思维导图

新增 `ai_results`，迁移版本 `0003`。仅后端，无本地大模型依赖。已有 PostgreSQL / Redis 就绪后，
在 `backend/` 执行（API 与 Worker 分别使用一个终端）：

```sh
uv sync --locked
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

服务端配置示例（可放入现有根目录 `.env`；本阶段未修改根目录配置模板）：

```dotenv
LLM_PROVIDER=auto
LLM_API_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=
LLM_MODEL=gpt-4o-mini
LLM_TIMEOUT_SECONDS=90
AI_TASK_TIMEOUT_SECONDS=900
LLM_CHUNK_CHARS=12000
LLM_MAX_CHUNKS=64
LLM_MAX_OUTPUT_TOKENS=4096
SUMMARY_PROMPT_VERSION=summary-v1
MINDMAP_PROMPT_VERSION=mindmap-v1
MINDMAP_SCHEMA_VERSION=tree-v1
# 可选，填实际服务/模型的每百万 Token 美元单价；未配置时费用返回 null
# LLM_INPUT_COST_PER_MILLION=0.00
# LLM_OUTPUT_COST_PER_MILLION=0.00
```

`auto` 无 Key 时选择确定性的 `heuristic-v1`，有 Key 时选择 OpenAI 兼容服务。
可显式设置 `LLM_PROVIDER=heuristic` 或 `openai_compatible`。启发式模式按时间段抽取源文稿，
不具备生成式推理或翻译能力；`language` 控制标签，源语言摘录保持原文，Token 与费用为零。
无 LLM Key 不影响本地总结闭环；若视频也无字幕，仍须按阶段 5 配置真实 ASR 或显式 Mock。
Mock ASR 内容不代表真实识别。首次公开 URL 的解析与字幕获取仍需要网络。

云端适配器发送 `<LLM_API_BASE_URL>/chat/completions`，使用 `messages`、`response_format=json_object`、
`temperature=0` 和 `max_tokens`，并向模型提供 JSON Schema。兼容服务须支持这些参数。
显式连接超时 5 秒、读取最多 60 秒、写入 30 秒；默认单次操作含有限重试总计不超过 90 秒。
仅网络错误/超时、429 与部分 5xx 最多重试一次；鉴权错误不重试。
响应限制 2 MiB，禁用重定向和隐式代理，校验 DNS 并固定实际连接 IP，禁止私网云 API。
上游响应、Key 不写入任务错误。云 API 经 HTTP 契约测试，未使用真实付费凭据调用。

每个请求由统一 TaskService 创建任务，在 Celery 内自动获取字幕/转写。
现有转写缓存优先；无缓存则在同一个 AI 任务内执行阶段 5 流水线，避免 Solo Worker 等待子任务。
`language` 是 AI 输出语言，字幕源语言自动选择。字幕、按需音轨与 ASR 保留原有取消、限额与临时清理机制。
AI 总时限覆盖转写与模型操作；模型操作每 500ms 检查取消，HTTP 取消会关闭请求。
最终任务完成与 AI 记录注册在同一个 CAS 事务中；取消已获胜时不写入新 AI 结果。
已完成的中间转写/总结缓存可保留供后续任务复用。

长文稿按时间戳段落切分，超长单段也切分；每段 Map 后按字符预算分层 Reduce。
最终保留所有 Map 章节证据，时间戳统一 `HH:MM:SS`，缺失字段/非法时间戳依据源段落修复后严格校验。
字符阈值是源内容预算，API 消息还包含 JSON 与 Schema 开销；超过最大 Chunk 数明确报错。
思维导图使用总结缓存，沿完整时间轴最多聚合为 40 组，输出递归 JSON 树与转义后的 Markdown。
树限制 500 节点/8 层；Markdown 前三级用标题，更深用嵌套列表。

缓存键严格为 `source_content_hash + result_type + llm_model + prompt_version + output_language`。
模型字段使用服务端配置的逻辑模型名；更换其实际模型版本时应更新模型名或 prompt 版本以失效缓存。
思维导图版本组合为 `mindmap-v1:tree-v1`，因此 Schema 版本变化也使缓存失效。
相同正文跨视频可复用同一结果，结果关联首次生成的转写记录；`source_basis` 描述该记录来源。
思维导图行仅统计其自身生成调用；预处理总结的用量单独记录在 summary 行，避免重复计费。
费用仅依赖服务端单价配置，不假设固定供应商价格；上游缺少 usage 时 Token/费用为 null。

验证（替换为当前公开可用的视频地址）：

```sh
VIDEO_URL='https://www.youtube.com/watch?v=BaW_jenozKc'
curl --max-time 40 -X POST http://127.0.0.1:8000/api/v1/ai/summaries \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"$VIDEO_URL\",\"language\":\"zh\"}"

# 用响应中的 task_id 替换，终态自动关闭；长任务超过 300 秒时重新连接
TASK_ID='替换为task_id'
curl -N --max-time 310 "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/events"
curl --max-time 5 "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID"

# completed 后从 result_payload.result_id 读取全文、章节、来源和用量
RESULT_ID='替换为result_id'
curl --max-time 5 "http://127.0.0.1:8000/api/v1/ai/results/$RESULT_ID"

curl --max-time 40 -X POST http://127.0.0.1:8000/api/v1/ai/mindmaps \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"$VIDEO_URL\",\"language\":\"zh\"}"

# 在另一个终端取消仍在运行的任务
curl --max-time 5 -X POST "http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/cancel"

uv run alembic check
uv run pytest
```

在 [Swagger](http://127.0.0.1:8000/docs) 的 `ai` 分组也可依次创建任务、查询结果。
重复相同请求将复用相同 `result_id`，任务结果标记 `cache_hit=true`；更换输出语言会使用独立缓存。
思维导图 GET 返回 `content_json` 与 `markdown_export`，后者可保存为 `.md`。

阶段 6 文件清单（均位于 `backend/`）：

- 新增 `alembic/versions/0003_ai_results.py`、`tests/test_ai_module.py`、`app/infrastructure/http_client.py`。
- 新增 `app/modules/ai/`：`__init__.py`、`models.py`、`schemas.py`、`repository.py`、`service.py`、
  `router.py`、`worker.py`、`summarizer.py`、`mindmap.py`；`providers/` 下的 `__init__.py`、
  `base.py`、`heuristic.py`、`openai_compatible.py`。
- 修改 `app/core/config.py`、`app/infrastructure/database/models.py`、
  `app/infrastructure/queue/celery_app.py`、`app/api/v1/router.py`。
- 修改 `app/modules/tasks/repository.py`、`service.py`，新增受取消保护的中间写入契约与 AI 任务类型。
- 修改 `app/modules/transcript/service.py`、`worker.py`、`providers.py`，复用转写流水线及共用受保护 HTTP 传输。
- 修改本 `README.md`。本阶段无新增依赖，无前端变更。

## 阶段 9：微信公众号口令登录

新增个人公众号 6 位动态口令登录、mock 联调、取关拦截、JWT Header/Cookie 鉴权及用户任务隔离。
完整配置、启动命令、curl / Swagger 验收、迁移与文件清单见 [阶段 9 交付说明](STAGE9.md)。

### 千问 ASR 长音频大小限制修复

`ASR_MAX_UPLOAD_MB` 限制每个云端请求的音频大小。千问适配器先将源音频转换为单声道 16kHz PCM，并按最多 120 秒分段；配置更小的上传上限时，自动按 32,000 字节/秒及 WAV/分段边界余量缩短切片。源音频不再因超过单次上传上限而在分段前失败，仍受 `MAX_FILE_SIZE_MB` 的任务文件上限与原有转换输出体积监控约束。每个切片上传前仍检查大小。

修改：`app/modules/transcript/providers.py`、`tests/test_media_and_transcript.py` 和本说明。完整回归：`AUTH_REQUIRED=False WECHAT_MP_STATIC_QR_URL= uv run pytest -q`，236 项通过，包含真实 FFmpeg 分片、时间轴连续性、取消/上游失败后的清理与源文件保留。

启动仍使用项目根目录 `python3 scripts/dev.py --skip-install`（或前文 API / Celery 独立启动命令）。重启 Worker 后，在工作台对原视频重新点击“获取字幕 / 转写”，或在 Swagger `/docs` 调用现有转写创建接口再查询任务状态。已失败的历史任务不会自动改为成功。本次未调用真实云端 ASR 重试该用户视频；超长音频仍可能触发独立的 `ASR_TIMEOUT`，应按服务能力配置有限的总超时。

### 千问模型与请求格式兼容性验证

本地配置原使用 `qwen-audio-3.0-asr-flash`，真实接口返回 HTTP 400 / `UNSUPPORTED_FORMAT` / `format is empty`。相同接口、密钥及一秒生成静音的请求改用 `qwen3-asr-flash` 后返回 HTTP 200，本地 `.env` 已切换为该模型（凭据未修改）。请求显式提供 `input_audio.format`；上游 `UNSUPPORTED_FORMAT` 映射为 `ASR_AUDIO_FORMAT_UNSUPPORTED`，避免只显示泛化 API 错误。

修改：本地 `.env` 的 `ASR_MODEL`、`app/modules/transcript/providers.py`、`tests/test_media_and_transcript.py` 与本说明。完整后端回归 237 项通过。重启开发服务后，在工作台重新提交转写；静音探测仅验证请求被接受，不代表用户完整视频转写已成功。

### 任务历史删除与清除

单条删除：`DELETE /api/v1/tasks/history/{task_id}`；一键清除：`DELETE /api/v1/tasks/history`。均要求登录，成功返回 `{"data":{"deleted_count":1}}`，一键清除覆盖当前用户全部分页。其他用户或不存在的记录返回 `TASK_NOT_FOUND`，未登录返回 `AUTH_REQUIRED`。重复删除成功且计数为 0。

使用迁移 `0005_task_history_visibility.py` 增加 `tasks.history_hidden`，历史列表与计数排除已删除记录。只更新当前用户的历史可见性，不删除任务、媒体、转写/总结结果，不取消后台任务，也不撤销已有结果访问权限。前端保留任务跟踪并持久化本地历史隐藏标记，避免刷新响应或 SSE 让记录重新出现。

修改：上述迁移、`modules/tasks/{models,repository,history,router}.py`、`tests/test_history_and_security.py`；前端 `TaskHistory.tsx`、`Icon.tsx`、`stores/workspace.ts`、`api/{client,index}.ts`、`style.css` 及对应工作台/状态测试。

部署：`cd backend` 后执行 `uv run alembic upgrade head`，然后按前文启动 API/Worker，前端执行 `pnpm dev`；亦可从项目根目录执行 `python3 scripts/dev.py --skip-install`。Swagger `/docs` 登录授权后，先查询 `GET /api/v1/tasks`，删除单条再查询，最后测试清除全部及重复清除。使用自己的测试记录，历史清除不会清理磁盘文件。

验证：完整后端 238 项、前端 56 项通过；前端类型检查与生产构建通过。覆盖越权、未登录、分页清除、重复删除、运行任务保留、结果授权保留、删除失败保留记录及过期刷新响应防回流。

历史删除跨域修复：CORS 的 `allow_methods` 已加入 `DELETE`。此前浏览器对单条删除/清除历史发起的 OPTIONS 预检返回 400，Axios 因而显示 `NETWORK_ERROR`；接口本身的单元测试未覆盖该浏览器前置步骤。现新增 localhost/127.0.0.1 两个来源、两个删除路径的预检与认证错误响应测试，仍拒绝未授权来源。后端完整回归 242 项通过。重启 API 后可用 `curl -i -X OPTIONS http://127.0.0.1:8000/api/v1/tasks/history -H 'Origin: http://127.0.0.1:5173' -H 'Access-Control-Request-Method: DELETE' -H 'Access-Control-Request-Headers: authorization,content-type'` 检查预期 HTTP 200 与允许 DELETE 的响应头。
