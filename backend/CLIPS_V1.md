**智能切片 · 阶段 A 后端业务闭环（2026-11-01）**

R1 修复更新（2026-11-02）：进程登记/恢复、任务启动异常、临时音频保护及核心区间留白已调整，见 [CLIPS_R1.md](CLIPS_R1.md)。下面的阶段 A 记录保留；最新验证结果以 R1 文档为准，R2/R3 已知问题尚待后续批次。

实现范围为公开视频和私有视频的候选分析、局部精细定位、最多 5 条连续片段整批导出，以及任务、权限、额度、存储与清理集成。首版保留原画面比例，H.264/AAC MP4、最高 720p；处理显示方向、非正方形像素、变帧率和音轨起始偏移。不修改 frontend，不提供实时直播、人工编辑、字幕烧录或竖屏跟踪。

对应设计见 `SMART_CLIPS_PLAN.md`。分析与导出为两次独立报价确认。默认 `CLIPS_ENABLED=false`，不会因更新代码自动开放收费任务。没有配置定位模型时可以分析，但导出报价返回 `CLIP_TIMING_NOT_CONFIGURED`。

**行为与交付规则**

- 分析输入为 url / upload_id / transcript_id 三选一。原片已删除但文稿仍已授权时，可用 transcript_id 分析。音频来源也可生成文字候选，视频导出明确拒绝无真实视频轨的来源。
- 先复用文稿，否则平台字幕优先、ASR 兜底。分块生成原文编号候选，再做全局排序和去重；不把模型生成的任意秒数当作剪辑依据。策略默认 3 条、30–90 秒，最多 5 条；可按确认策略减少数量，非空结果才算分析交付。
- 粗文稿内的句子只获得字符范围，不按字数推算时间。所有正式导出均在候选附近提取小段音频，由独立时间戳模型重新定位；若不能可靠匹配、出现重复台词歧义、重叠或时长越界，明确失败，不静默粗切。
- 原文、原分段、精确 transcript_id、用户、策略、模型和版本共同确定分析缓存；精细定位绑定实际源文件 SHA-256。派生定位单独保存，不修改已有转写、引用、问答和总结。
- 新分析非空成功扣 1 次 AI；首次 ASR 按现有整段音轨时长扣一次。无候选、失败、有效取消零核销；已有文稿、字幕和缓存不重复扣转写。本人已交付的同版本方案免费复用。
- 导出不重复扣 AI 或全文转写；局部定位作为平台内部处理成本单独记录音频秒数。每账号每天最多 10 次新渲染，每方案每天最多 3 次；真正启动即占频率，失败/取消不退频率。已有成片缓存不占新渲染次数。其他任务、权益和下行流量限制继续生效。
- 无原视频时不能创建新渲染；同账号重新上传相同 SHA-256 文件后，重新报价可恢复。原片、方案、文字成果和成片各自保留生命周期；已有有效成片可在原片删除后继续下载及复用。
- 所有选中片段均成功、解码和音画时间校验通过后，才整体登记媒体、导出关联、任务完成和额度结算。任一片段失败不公布新文件。每条成片通过既有受控下载、短期凭证和 Range 分发，不能凭公开 video_id 或他人 ID 越权访问。
- `media_files.purpose` 区分 legacy/full_source/full_audio/clip，完整音轨和原视频复用排除 clip。历史 legacy 媒体保留兼容；切片读取旧缓存时仍会重新探测真实视频轨、时长和指纹，不仅相信旧记录。
- 一次导出只获取一次完整源视频，串行渲染。使用持久化容量预留、文件读锁、500ms Watchdog 和独立进程组。源媒体读保护清单在 Worker 死亡后仍能保护存活 FFmpeg 使用的公共源文件。
- 文件提升前先保存提交清单；数据库确认不明时核对状态，不盲删可能已完成的文件。部分提升失败、取消获胜及已确认死亡的任务可按清单回收未登记文件；原进程组状态不明时保留。

**配置与部署**

依赖仍由现有 pyproject.toml / uv.lock 管理，无新增 Python 依赖；系统需要 FFmpeg 和 ffprobe。新配置见 `clips.env.example`，加入 API 与 Worker 实际加载的服务端环境，两个进程必须一致。根目录配置模板未修改。

`CLIPS_TIMING_API_BASE_URL` 指向已验证兼容 `/audio/transcriptions` 的服务基础 URL，需支持 `response_format=verbose_json`、`timestamp_granularities[]=segment` 并返回真实 `segments`。密钥仅放服务端。某家服务支持 OpenAI 兼容文本接口，不表示其语音模型支持此格式；Qwen 现有粗分段不可当作本定位能力。

定位与渲染有独立总时限，仍受导出整体 1800 秒预算约束。默认预留最多 1536 MiB 临时峰值，原件、音频、全部成片一起计入 5 GiB 配额；空间不够在调用定位模型前失败。可按实际资源和已验证性能调整配置，不全局放宽原下载单文件限制。

新增迁移 `0011`，须先正常停止旧 API / Worker，再在对应环境升级并启动。以下命令供运行者使用，本轮没有迁移业务库或重启已有开发/生产服务；迁移验收使用隔离 PostgreSQL schema。

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv sync --locked
uv run alembic upgrade head
uv run alembic check
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

另一个终端，保持单个 Solo Worker：

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

恢复和清理仍使用已有独立周期任务：

```sh
uv run python -m app.modules.billing.manage reconcile
uv run python -m app.infrastructure.storage.cleanup
```

应用回退保留新增表。已有切片方案时 `0011` 拒绝有损 downgrade，不删除交付关系或授权。

**API 与 Swagger/curl 验收**

打开 `http://127.0.0.1:8000/docs`，登录授权，在 clips 分组操作。所有接口位于 `/api/v1`，成功包裹 data，错误保持 code/message/request_id。新增任务类型为 ai_clip_plan、clips_export，确认仍走 `/billing/tasks`；API 不执行 FFmpeg 或模型调用。

| 方法/路径 | 用途 |
| --- | --- |
| POST `/clips/plans/quotes` | 三选一来源、strategy、language，返回分析报价 |
| GET `/clips/plans/{id}` | 候选、原文范围、粗精度、版本、用量和源状态 |
| POST `/clips/exports/quotes` | plan_id、revision、1–5 个不同 candidate_ids、profile |
| GET `/clips/exports/{id}` | 成片列表、最终源区间、验证结果、有效期和下载可用性 |
| 既有 `/billing/tasks`、`/tasks/*`、`/files/*` | 确认、进度/SSE、取消、短期凭证、下载 |

先使用现有上传接口完成视频上传与探测，取得 ready 的 upload_id；或者直接使用公开视频 url；或者使用自己已经成功获得的 transcript_id。

```sh
BASE=http://127.0.0.1:8000/api/v1
TOKEN='替换为自己的登录凭证'
TRANSCRIPT_ID='替换为自己的文稿UUID'
curl --max-time 8 -X POST "$BASE/clips/plans/quotes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"transcript_id\":\"$TRANSCRIPT_ID\",\"strategy\":{\"count\":3,\"min_seconds\":30,\"max_seconds\":90},\"language\":\"zh\"}"

QUOTE_ID='替换为分析报价UUID'
curl --max-time 8 -X POST "$BASE/billing/tasks" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"quote_id\":\"$QUOTE_ID\"}"

TASK_ID='替换为分析任务UUID'
curl -N --max-time 310 -H "Authorization: Bearer $TOKEN" "$BASE/tasks/$TASK_ID/events"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/tasks/$TASK_ID"

PLAN_ID='替换为result_payload.clip_plan_id'
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/clips/plans/$PLAN_ID"
```

取 candidates 中真实存在的 ID，提交导出报价。profile 由服务端预设，不接受自定义编码参数。若 outcome=no_highlights 则没有可导出候选，核对额度已释放。

```sh
CANDIDATE_ID='替换为候选id'
curl --max-time 8 -X POST "$BASE/clips/exports/quotes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"plan_id\":\"$PLAN_ID\",\"revision\":1,\"candidate_ids\":[\"$CANDIDATE_ID\"],\"profile\":\"video_720_mp4\"}"
```

确认新 quote_id 后同样查询任务；完成后使用 result_payload.clip_export_id 读取成片，再使用每项 file_id 的既有下载接口。

```sh
EXPORT_ID='替换为成片批次UUID'
FILE_ID='替换为成片文件UUID'
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/clips/exports/$EXPORT_ID"
curl --max-time 5 -X POST -H "Authorization: Bearer $TOKEN" "$BASE/files/$FILE_ID/download/access"
curl --max-time 10 -H "Authorization: Bearer $TOKEN" -H 'Range: bytes=0-99' \
  "$BASE/files/$FILE_ID/download" -o /tmp/videohub-clip-range.bin
# 取消：POST /tasks/{id}/cancel；已成功任务不会被晚到取消覆盖。
```

另外核对：相同方案免费复用；相同有效成片不重新渲染/续期；另一账号访问被拒绝；源删除后文稿分析可用、新渲染要求原片；错误时间轴、音频来源导出、配额不足和模型不支持分段时间戳均明确失败。公网链接状态返回 public_link_requires_fetch，不能把数据库里的 URL 当作平台当前可访问性的保证。

**修改文件清单**

新增：

```text
backend/alembic/versions/0011_smart_clips.py
backend/app/modules/clips/__init__.py
backend/app/modules/clips/models.py
backend/app/modules/clips/schemas.py
backend/app/modules/clips/repository.py
backend/app/modules/clips/selection.py
backend/app/modules/clips/service.py
backend/app/modules/clips/source.py
backend/app/modules/clips/media.py
backend/app/modules/clips/media_child.py
backend/app/modules/clips/timing.py
backend/app/modules/clips/worker.py
backend/app/modules/clips/router.py
backend/tests/test_clips.py
backend/tests/test_clips_postgres.py
backend/tests/test_clips_celery.py
backend/clips.env.example
backend/CLIPS_V1.md
```

修改：

```text
backend/app/core/config.py
backend/app/api/v1/router.py
backend/app/infrastructure/database/models.py
backend/app/infrastructure/queue/celery_app.py
backend/app/infrastructure/process_runner.py
backend/app/infrastructure/storage/base.py
backend/app/infrastructure/storage/local.py
backend/app/infrastructure/storage/cleanup.py
backend/app/modules/admin/router.py
backend/app/modules/ai/providers/base.py
backend/app/modules/ai/providers/heuristic.py
backend/app/modules/billing/manage.py
backend/app/modules/billing/repository.py
backend/app/modules/billing/service.py
backend/app/modules/billing/worker.py
backend/app/modules/download/worker.py
backend/app/modules/media/models.py
backend/app/modules/media/repository.py
backend/app/modules/tasks/service.py
backend/app/modules/tasks/repository.py
backend/app/modules/tasks/history.py
backend/app/modules/uploads/repository.py
backend/app/modules/video/repository.py
backend/README.md
```

工作区原有其他改动保留；frontend、pyproject.toml、uv.lock 和根目录环境模板未修改。

**验证命令与边界**

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run pytest
uv run pytest tests/test_clips.py tests/test_clips_postgres.py tests/test_clips_celery.py -q
uv run python -m compileall -q app
```

测试覆盖真实 FFmpeg/ffprobe 导出与解码、长关键帧间隔的精确切点、音轨延迟、非零起始 PTS、变帧率、旋转、像素比例；候选引用校验、空结果零核销、失败/取消释放、跨账号访问、Range 和已交付结果复用；部分文件提升和提交回复丢失；存储预留和存活进程源保护；真实 PostgreSQL 并发确认及切片专用结算；真实 HTTP + Redis + Celery Solo 的上传→ASR→分析→两条视频导出→SSE→下载→删除原片后复用。

ASR/LLM 在测试中明确模拟；真实媒体处理、数据库、队列和文件分发运行实际实现。未调用付费模型，未证明真实供应商的时间戳精度或精选接受率；生产收费分析拒绝 heuristic。定位 audio_seconds 统计逻辑请求输入，不能当作供应商重试后的最终账单。默认关闭开关，在供应商兼容性、真实样本质量和部署机器预算验证后再启用。

**本轮最终验证结果**

- 修改前基线：418 passed；最终执行 `uv run pytest`：**447 passed，158.16 秒**，新增 29 项切片相关测试。
- 真实 HTTP + 独立 Celery Solo、Redis、PostgreSQL schema 完成上传、模拟 ASR/LLM、两条真实视频导出、SSE、Range、授权及原片删除后的成片复用；临时 API/Worker 已停止。
- PostgreSQL 隔离 schema 验证 `0011` 升级、兼容旧来源、`alembic check` 与拒绝有损回退；业务库未迁移。
- `uv run python -m compileall -q app`、四个切片 OpenAPI 路径、`clips.execute` 注册和后端 `git diff --check` 通过；新增文件额外检查无行尾空白。
- 相比本轮开始时的内容指纹，frontend、pyproject.toml、uv.lock 均未变化；没有重启已有开发/生产服务或调用付费 API。
