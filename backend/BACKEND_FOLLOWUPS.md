# 后端前三项完善 · 2026-11-01

本次修复清理批次遗漏、短暂存储锁冲突导致任务失败，并新增基于已授权文稿的总结 / 思维导图入口。
不修改 frontend，不新增依赖或数据库迁移，不调用付费 ASR / LLM，不改写现有业务数据。

## 修复内容

### 1. 过期上传分页清理

上传记录按 `(expires_at, id)` 排序，以稳定游标每页读取 100 条。即使前一页文件全部被任务占用，也继续扫描后面的记录。
删除成功后不使用会移动位置的 OFFSET；以固定扫描时间排除本轮之后新建的记录。
取得文件独占锁后，在 uploads Repository 的事务内重新检查过期时间与状态，防止探测刚成功续期的文件被旧快照误删。
仍保留活跃任务、文件读锁和子进程组保护，重复清理不重复统计回收。

### 2. 短暂存储争用等待

新增 `infrastructure/storage/retry.py`。下载、音频提取、转写、媒体探测和清理的存储准入支持异步等待：

- 每次资源获取最多等待 5 秒，每 100ms 轮询，等待期间检查取消。
- 任务通过现有 SSE 发布 `waiting_for_storage`，不新增任务状态枚举。
- 只重试获取存储锁 / 预留容量的入口；不重复执行已开始的下载、FFmpeg、ASR、LLM 或结果写入。
- 仅重试暂时性的 `STORAGE_BUSY / 503`。配额不足、磁盘不足、重复预留 409、处理主体错误立即按原规则处理。
- 持续繁忙超过预算返回 STORAGE_BUSY；取消返回 TASK_CANCELLED，均沿用原来的终态 CAS 与额度释放机制。

### 3. 基于文稿的 AI 任务

以下接口新增 `transcript_id`：

- `POST /api/v1/billing/quotes`，仅限 `action=ai_summary` 或 `ai_mindmap`。
- `POST /api/v1/ai/summaries`、`POST /api/v1/ai/mindmaps`。

AI 输入必须在 `url`、`upload_id`、`transcript_id` 中三选一。转写 / 下载仍只接受原有媒体来源；不能用文稿创建下载或再次转写。
开启额度时仍须先报价再确认，直接调用 AI 创建接口不能绕过报价。

文稿来源在报价、首次确认与 Worker 执行时验证授权；排队期间还会检查文稿 ID、来源 video_id 和内容哈希是否匹配。
无需原始音视频文件、平台解析、音频下载、字幕提取、ASR 或转写额度。原文件已删除、过期、模型配置已变更，仍可以分析已获得的文稿。
已有会员视频的文稿不受重新下载的文件大小 / 时长门槛约束；AI 字数（60000）、调用规模、账号状态、并发和频率规则继续生效。
保留所选文稿的 content_version，禁止偷偷重新转写成另一版文稿。

报价 `maximum.asr_seconds=0`，新总结 / 导图成功消耗原有 AI 次数；失败 / 取消零核销。
自己的相同已交付结果报价为零 AI，仍经 Worker 重新验证文稿授权。组合总结 + 导图保持一次 AI 交付。
缓存绑定精确的文稿与时间轴。可兼容复用旧公共缓存，但仅在其 transcript_id 与请求完全相同时；相同文本、不同时间轴不会被误用。
已确认免费复用的结果若丢失，则失败并要求重新报价，不以零报价重新调用模型。

## Swagger / curl 验证

Swagger：`http://127.0.0.1:8000/docs`，使用已有登录账号的 Bearer Token 授权。
在 billing 分组选择已成功获得的 transcript_id，action 选 ai_summary / ai_mindmap；不要同时填写 URL 或 upload_id。
先确认返回的 maximum 中 asr_seconds 为 0，再使用 quote_id 创建任务。

```sh
BASE=http://127.0.0.1:8000/api/v1
TOKEN='替换为自己的 Bearer Token'
TRANSCRIPT_ID='替换为自己的已授权文稿 UUID'
curl --max-time 5 -X POST "$BASE/billing/quotes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"transcript_id\":\"$TRANSCRIPT_ID\",\"action\":\"ai_summary\",\"language\":\"zh\"}"
QUOTE_ID='替换为返回的 quote_id'
curl --max-time 5 -X POST "$BASE/billing/tasks" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"quote_id\":\"$QUOTE_ID\"}"
TASK_ID='替换为返回的 task_id'
curl -N --max-time 310 "$BASE/tasks/$TASK_ID/events" -H "Authorization: Bearer $TOKEN"
curl --max-time 5 "$BASE/tasks/$TASK_ID" -H "Authorization: Bearer $TOKEN"
```

将 action 改为 ai_mindmap 可生成总结与导图。用任务结果 result_id 读取 `/api/v1/ai/results/{id}`。
重复获取相同已成功结果应为零 AI 报价；换账号请求文稿应拒绝；原文件删除后使用 transcript_id 应仍成功。

清理回归使用 SQLite 与真实 PostgreSQL 临时 schema 构造超过 100 条记录，验证占用跳过、相同过期时间的 ID 排序、删除后不漏页及续期保护。
存储争用回归验证释放锁后成功、5 秒策略的缩短测试预算、取消、额度释放，以及处理主体不会被重放。

## 启动命令

无需新增迁移。尚未执行上一轮 0010 的环境，仍应先升级至 head；已有 API / Worker 需要正常停止后加载新代码。

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv sync --locked
uv run alembic upgrade head
uv run alembic check
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

另一终端：

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

## 本次修改文件

新增：

- `app/infrastructure/storage/retry.py`
- `tests/test_backend_followups.py`
- `tests/test_backend_followups_postgres.py`
- `BACKEND_FOLLOWUPS.md`

修改：

- `app/infrastructure/storage/cleanup.py`
- `app/modules/uploads/{repository,cleanup,worker}.py`
- `app/modules/download/worker.py`
- `app/modules/transcript/{service,worker}.py`
- `app/modules/video/sources.py`
- `app/modules/ai/{router,service,repository,worker}.py`
- `app/modules/billing/{router,service,repository,worker}.py`
- `tests/test_uploads_celery.py`
- `README.md`、`UPLOADS_V1.md`

## 测试结果

- `uv run pytest -q`：**409 passed**，62.75 秒；相比上一轮新增 18 个测试，并扩展既有真实 HTTP + Celery Solo 联调。
- 联调完成删除原文件后按 transcript_id 生成新总结、读取结果、相同结果零 AI 复用，并核对转写额度未变化。
- `uv run python -m compileall -q app`、`git diff --check`、Swagger OpenAPI 的三选一输入契约检查通过。
- PostgreSQL 验证使用临时 schema，Redis 与 API / Worker 使用隔离测试实例；ASR / LLM 使用明确模拟或 MockTransport，未调用付费模型。
