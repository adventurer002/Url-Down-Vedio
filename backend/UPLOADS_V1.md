# 本地视频与音频 · 后端业务闭环

支持公网链接、本地视频和 MP3、M4A 共用现有文稿、总结、思维导图、问答及个人知识库。
本次不修改前端；上传原文件保持私有，音频识别依赖服务端配置的 ASR。
视频分析基于语音内容，未引入视频画面理解、OCR 或多轮聊天。

## 行为与范围

- 输入格式：MP4、MOV、MKV、WebM 视频和 MP3、M4A 音频。必须通过真实容器、音轨和时长校验；音频的封面图片不当作视频轨；M4A 支持 AAC、ALAC 编码。
- 先登录，再创建上传会话；每次上传一个文件。HTTP 流式收取字节，不在 HTTP 进程运行 ffprobe、FFmpeg、ASR 或 LLM。
- 上传完成后，通过统一 TaskService 创建 `media_probe`，由 Celery Solo 执行本地媒体探测。重复 complete 只对应同一个任务，Redis 派发不确定时保留队列，由 reconcile 重派。
- 视频抽取单声道 16 kHz WAV 送入 ASR；M4A 先在 Worker 中规范为单声道 16 kHz WAV，MP3 直接交给 ASR 适配器，由供应商适配器按需转换、切片。上传文件不调用 yt-dlp、不请求平台字幕。
- 转写、总结、导图先报价，再确认；总结及导图任务可自动获取文稿。问答继续以已授权 `transcript_id` 为入口，每个新问题成功扣一次 AI，不重复转写。
- 上传、探测不扣转写分钟或 AI 次数。ASR 按完整真实音轨时长向上取整一次，沿用实际核销与失败/取消释放；自己的转写和 AI 成果按现有规则复用。
- 默认非会员 256 MiB / 20 分钟，会员 512 MiB / 60 分钟，仍受 MAX_FILE_SIZE_MB 限制。每账号最多 3 个未完成上传、每小时 20 个新上传；后续任务仍受现有任务频率和额度规则约束。
- 首版不提供批量上传、分片续传、用户自定义代理、Cookie 或服务器本地路径输入。失败的传输可在会话有效期内从头重传；校验任务失败后需新建上传。

## API

所有路径位于 `/api/v1`，成功包裹 `data`，错误包含 `error.code/message/request_id`。
上传接口始终要求登录，即使开发时关闭 ALLOWANCES_ENABLED。

| 方法 / 路径 | 请求 / 行为 |
| --- | --- |
| POST `/uploads` | JSON：`filename`、`size_bytes`，返回 201 和上传 `id` |
| PUT `/uploads/{id}/content` | `application/octet-stream` 原始文件字节，禁止把 multipart 外壳当作文件上传 |
| POST `/uploads/{id}/complete` | 创建或取回媒体探测任务，202，返回 task_id / events_url |
| GET `/uploads/{id}` | 查询文件大小、哈希、状态、过期时间、来源 video_id、探测 task_id、媒体类型及时长 |
| DELETE `/uploads/{id}` | 删除原文件；有未结束任务或存活子进程组时返回 UPLOAD_IN_USE，先取消并等待终态 |
| POST `/billing/quotes` | 在原有 action/language 等字段外，传 `url`、`upload_id` 或 `transcript_id`，必须三选一；文稿仅支持总结 / 导图 |
| POST `/billing/tasks` | 沿用原 `quote_id` 确认；首次确认重新检查文件，重复确认仍返回原任务 |
| POST `/transcriptions`、`/ai/summaries`、`/ai/mindmaps` | 转写支持 url / upload_id 二选一，AI 另支持 transcript_id；开启额度时仍要求走报价确认，不能绕过计费 |
| GET `/tasks/{id}`、`/tasks/{id}/events`、POST `/tasks/{id}/cancel` | 沿用任务归属校验、SSE 和协作取消 |
| `/knowledge/*` | 问答、引用、收藏与搜索沿用现有接口，支持上传来源 |

上传的状态依次为 pending → uploaded → validating → ready；异常状态为 failed、expired、deleted。
GET 同步反映已取消/失败/过期的探测任务。尚未就绪、文件已删除或过期时不能产生新的文件处理报价。
原有 URL 调用保持兼容；本地文件来源不提供平台下载档位或下载任务报价。

## 数据与隔离

- 新增 `uploads`，归 uploads 模块持有；文件按服务端 UUID 放在 `storage/uploads/{id}/source.bin`，原始文件名仅用于展示。
- videos 增加 source_kind / owner_id / content_sha256 / media_type；本地来源没有平台 ID 或伪造 URL。保留内部 video_id，避免重写既有任务、转写、问答和收藏外键。
- 公网来源保留 `(platform, platform_video_id)` 唯一约束；私有内容按 `(owner_id, content_sha256)` 唯一。SHA-256 在服务端接收过程中计算，相同账号重复上传可以复用来源与文稿，不同账号独立。
- AI 缓存新增 cache_scope。旧数据为 public；私有范围包含账号及确切 transcript_id，避免相同文本来自不同录音、时间轴或用户时绑定错误原文。原模型、提示词版本、语言等缓存维度保持不变。
- 所有模块通过 Repository / Service 契约交互，媒体探测、音频获取、ASR、LLM 和存储具有 Protocol 契约。
- 历史与收藏的来源元信息增加 source_kind / media_type；平台与 URL 在上传来源中可为 null，前端接入时需按来源分支展示。

## 存储、取消与恢复

原文件默认保留 24 小时；空上传会话默认 1 小时。原文件过期或删除不删除文稿、AI 结果、已成功问答或收藏。
这些已授权文字成果继续可读，可继续问答，也可通过 transcript_id 新建总结 / 导图；需要重新转写时重新上传。

上传及处理使用持久化容量预留，原文件、处理目录、ASR 分段共同计入 STORAGE_MAX_QUOTA_MB=5120，并保留 STORAGE_MIN_FREE_MB。
接收前预留容量，接收中校验实际字节数及可用空间；容量不足不会调用付费能力。
下载/转写长任务不再持有全局存储互斥锁，新的上传可在已有任务处理时接收。
上传原文件与复用的公共媒体使用独立读锁，清理不能删除正在读取的文件。

上传接收有空闲超时和总超时；中断或长度不符删除半成品，可在会话有效期内重新发送。
文件原子落盘后若数据库确认结果不明，保留文件供 GET 恢复，不误删可能已成功提交的内容。
探测/转码不做自动重试，采用显式总时限；ASR/LLM 沿用原有连接超时、总超时与有限重试。
Qwen ASR 的临时切片位于任务临时目录，不写进上传原文件目录；供应商单次请求体上限与原文件体积上限分别生效。

外部子进程继续使用独立进程组及 500ms Watchdog。取消、超时或体积越界时 TERM → KILL，确认退出后清理任务目录；数据库 CAS 防止晚到取消覆盖成功。
探测的来源注册与任务完成在同一事务内。进程组不能确认退出时保留目录和源文件，禁止提前回收。
Worker 崩溃恢复沿用 reconcile，增加未计费探测任务的恢复与重派，并释放已确认死亡任务的磁盘预留。

部署时继续独立定期执行：

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run python -m app.modules.billing.manage reconcile
uv run python -m app.infrastructure.storage.cleanup
```

## 配置与启动

依赖沿用 pyproject.toml / uv.lock，无新增 Python 依赖。系统需要 FFmpeg 和 ffprobe。
`.env.example` 与 `.env.docker.example` 均补充相同上传配置，保留各自 localhost / 容器网络连接地址。

| 配置 | 默认值 |
| --- | --- |
| UPLOAD_IDLE_TIMEOUT_SECONDS | 30 |
| UPLOAD_TOTAL_TIMEOUT_SECONDS | 600 |
| UPLOAD_SESSION_TTL_SECONDS | 3600 |
| UPLOAD_PROBE_TIMEOUT_SECONDS | 20 |
| UPLOAD_MAX_PENDING_PER_USER | 3 |
| UPLOAD_MAX_PER_HOUR | 20 |

真实转写沿用已有 ASR_API_KEY / ASR_MODEL / ASR_PROVIDER 配置；长音频需按供应商延迟配置 ASR_TOTAL_TIMEOUT_SECONDS。
生产环境拒绝 Mock ASR 和摘录模型作为收费 AI 能力。本次测试未调用付费服务，不以离线结果代表识别或模型质量。

迁移为新增 `0010`。先正常停止旧 API / Worker，再启动新版本，避免旧 Worker 不认识 uploads.probe。
生产环境仍按既有备份与发布流程操作；本次迁移验证在临时 schema 内完成，不迁移业务库。

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv sync --locked
uv run alembic upgrade head
uv run alembic check
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

另一个终端：

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

`backend/deploy/openresty-api.conf` 新增上传 location：512 MiB 请求体上限、关闭请求体代理缓冲、上传响应超时 610 秒，以及统一 413 JSON。
需将该片段应用至实际 OpenResty server 并通过 `nginx -t`（或 `openresty -t`）检查配置；本机未安装 OpenResty，未执行该服务器配置检查。不把 storage/uploads 暴露为静态目录。

## Swagger / curl 验收

Swagger 位于 `http://127.0.0.1:8000/docs`。先以现有账号授权，在 uploads 分组创建会话、上传二进制、complete；在 tasks 查询探测完成，再获取报价并确认。
输入视频时把下面固定展示名改为对应视频扩展名。

```sh
BASE=http://127.0.0.1:8000/api/v1
TOKEN='替换为已登录账号的 Bearer Token'
FILE='/绝对路径/recording.mp3'
SIZE=$(wc -c < "$FILE" | tr -d ' ')
curl --max-time 5 -X POST "$BASE/uploads" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"filename\":\"recording.mp3\",\"size_bytes\":$SIZE}"

UPLOAD_ID='替换为返回的 id'
curl --max-time 610 -X PUT "$BASE/uploads/$UPLOAD_ID/content" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/octet-stream' \
  --data-binary @"$FILE"
curl --max-time 5 -X POST "$BASE/uploads/$UPLOAD_ID/complete" -H "Authorization: Bearer $TOKEN"
PROBE_TASK_ID='替换为校验 task_id'
curl --max-time 5 "$BASE/tasks/$PROBE_TASK_ID" -H "Authorization: Bearer $TOKEN"
curl --max-time 5 "$BASE/uploads/$UPLOAD_ID" -H "Authorization: Bearer $TOKEN"
```

等待 ready 后报价。action 可为 transcription、ai_summary 或 ai_mindmap；ai_mindmap 沿用总结与导图组合交付。

```sh
curl --max-time 5 -X POST "$BASE/billing/quotes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"upload_id\":\"$UPLOAD_ID\",\"action\":\"ai_mindmap\",\"language\":\"zh\"}"
QUOTE_ID='替换为报价 quote_id'
curl --max-time 5 -X POST "$BASE/billing/tasks" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"quote_id\":\"$QUOTE_ID\"}"
TASK_ID='替换为处理 task_id'
curl -N --max-time 310 "$BASE/tasks/$TASK_ID/events" -H "Authorization: Bearer $TOKEN"
curl --max-time 5 "$BASE/tasks/$TASK_ID" -H "Authorization: Bearer $TOKEN"
TRANSCRIPT_ID='替换为结果 transcript_id'
curl --max-time 5 "$BASE/transcriptions/$TRANSCRIPT_ID" -H "Authorization: Bearer $TOKEN"
```

问答与收藏：

```sh
curl --max-time 8 -X POST "$BASE/knowledge/questions/quotes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"transcript_id\":\"$TRANSCRIPT_ID\",\"question\":\"主要内容是什么？\",\"language\":\"zh\"}"
# 返回的新报价仍通过 POST /billing/tasks 确认。
curl --max-time 8 -X POST "$BASE/knowledge/library" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"transcript_id\":\"$TRANSCRIPT_ID\",\"tags\":[\"录音\"]}"
curl --max-time 5 "$BASE/billing/allowances" -H "Authorization: Bearer $TOKEN"
```

取消使用 POST `/tasks/{id}/cancel`，等待 cancelled 后删除上传。已完成任务的取消不覆盖结果。
另外验证：不同账号读取/删除 upload_id 返回 404；伪装格式、无音轨、超体积、超时长应失败且零核销；删除原文件后仍能读文稿及进行问答。

## 验证命令与修改文件清单

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run pytest
uv run python -m compileall -q app
```

新增：

- `app/modules/uploads/{__init__,models,repository,storage,service,probe,worker,router,cleanup}.py`
- `app/modules/video/sources.py`
- `alembic/versions/0010_private_media_uploads.py`
- `tests/test_uploads.py`、`tests/test_uploads_postgres.py`、`tests/test_uploads_celery.py`
- `UPLOADS_V1.md`

修改：

- `app/modules/video/{models,repository,resolver}.py`
- `app/modules/transcript/{router,service,worker,providers}.py`
- `app/modules/ai/{models,repository,router,service,worker}.py`
- `app/modules/media/audio.py`、`app/modules/download/worker.py`
- `app/modules/billing/{router,service,repository,manage}.py`
- `app/modules/tasks/{repository,service}.py`
- `app/infrastructure/storage/{base,local,cleanup}.py`
- `app/infrastructure/{process_runner.py,database/models.py,queue/celery_app.py}`
- `app/{main.py,api/v1/router.py,core/config.py}`
- `deploy/{openresty-api.conf,production.env.example}`、`README.md`
- 根目录 `.env.example`、`.env.docker.example`

专项测试使用真实 FFmpeg/ffprobe、PostgreSQL 临时 schema 和独立 Redis；真实 HTTP + Celery Solo 测试完成上传、异步探测、ASR、总结+导图、SSE、删除与文稿保留。ASR/LLM 使用明确模拟或 MockTransport。
完整测试结果见本次交付记录。

## 本次交付结果（2026-11-01）

- `uv run pytest -q`：**391 passed**（原有 360 项 + 新增 31 项），最终全量运行 45.91 秒。
- 真实 HTTP + Celery Solo 完成 MP3 上传 → 探测 → ASR → 总结与导图 → SSE → 删除原文件后读取文稿。
- PostgreSQL 临时 schema 验证并发任务绑定、取消与来源注册 CAS、用户级去重、迁移兼容与 `alembic check`；检测到上传记录或私有来源时拒绝有损 downgrade。应用回退应保留新增表。
- `uv run python -m compileall -q app`、Swagger OpenAPI 上传/报价契约、Celery `uploads.probe` 注册及 `git diff --check` 通过。
- frontend 改动与本次开始前一致；pyproject.toml / uv.lock 未修改；未迁移现有业务 schema，未调用付费模型，临时 API / Worker 已停止。
- 本机没有 OpenResty / nginx，服务器配置需部署时执行 `nginx -t` 或 `openresty -t` 验证。

后续完善：清理分页、存储锁有界等待与文稿直接生成 AI 的接口和最新测试结果见 [BACKEND_FOLLOWUPS.md](BACKEND_FOLLOWUPS.md)。

## M4A 格式支持

新增 `.m4a` 输入，接受 AAC 和 ALAC 音轨。仍需实际容器校验和音轨校验，改名为 `.m4a` 的 MP3 或含真实视频轨的 MP4 会被拒绝；封面图不视为视频轨。沿用现有文件大小、时长、超时、任务取消和额度约束，无需新增数据库迁移。

M4A 通过现有 `TranscriptionAudioProvider` 契约在 Celery Worker 内用有界 FFmpeg 规范为 WAV，再送 ASR。OpenAI 兼容 ASR 按实际 WAV / MP3 发送文件名与 MIME，避免将 WAV 错标为 MP3。原文件不改写，临时 WAV 随任务清理。

验证：真实 AAC 和 ALAC M4A 的上传、探测、转写与缓存；M4A 的总结、导图、问答、收藏和原文件删除后复用；伪装格式拦截；真实 HTTP + Solo Worker 的 MP3 / M4A 两条链路。测试使用模拟 ASR 和本地总结器，无付费调用。全量后端测试 418 项通过。

本次修改：`app/modules/uploads/service.py`、`app/modules/uploads/probe.py`、`app/modules/media/audio.py`、`app/modules/transcript/worker.py`、`app/modules/transcript/providers.py`；测试为 `tests/test_uploads.py`、`tests/test_uploads_celery.py`、`tests/test_media_and_transcript.py`。启动命令和 curl / Swagger 步骤沿用本文前述流程，将上传文件名替换为实际 M4A 文件名，并按真实字节数提交即可。
