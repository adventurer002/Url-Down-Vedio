# 下载与 AI 容量验收

目标机器为轻量云 4 核 / 8 GB / 10 Mbps。沿用两个 media Solo 槽及各一个 control、ASR、LLM、render 槽。2026-11-20 只读核对了目标机。系统识别到 4 个逻辑 CPU、7425 MiB 内存、未配置 Swap，根盘剩余约 101 GiB。API、PostgreSQL、Redis 就绪。运行中的后端单元只有 API 和旧版 Solo Worker。公网 10 Mbps 为已知套餐规格，云内对象上传路径和公网持续吞吐待实测。

## 准备隔离环境

- 使用经确认的版本副本、独立 PostgreSQL 数据库、Redis 实例、JWT 密钥、测试账号、存储目录和 loopback API 端口。在独立库执行迁移到当前 Alembic head（含 0016 和 0017）。同机 API、Worker 和磁盘的压力会占用机器资源，按获准窗口执行。
- 开启 `AUTH_REQUIRED=true`、`ALLOWANCES_ENABLED=true`、`STAGED_TASKS_ENABLED=true`，使用 `ASR_CHUNK_CONCURRENCY=1`。FFmpeg 沿用单线程，以媒体槽、独立渲染槽和公网出口作为主要观测点。记录实际开关、下载和 AI 超时、模型、供应商配额、Git 版本及样本清单。
- 为十个独立账号准备测试权益。业务规则下，免费用户的视频上限为 20 分钟，每日下载与容量测试也共用额度。40/60 分钟、重复波次及缓存对照需要足够的隔离会员测试权益。已有文稿必须由对应账号预先持有。短样本跑通后，按确认的预算接入真实 ASR 和 LLM。
- 第一轮用本地交付作基线，关闭对象镜像和分发。对象轮在独立私有桶/前缀开启镜像及独立出口网关。按 [PERFORMANCE_C.md](PERFORMANCE_C.md) 配置云内上传端点和网关。两轮用相同来源、档位、模型参数，并为冷任务使用独立初始缓存状态。

在 backend 目录执行。环境文件和运行目录使用本机隔离路径。提供独立的 `DATABASE_URL`、`REDIS_URL`、`CELERY_BROKER_URL`、`STORAGE_LOCAL_PATH` 和测试 JWT 配置。

```bash
export VIDEOHUB_D_ENV=/path/to/private/acceptance.env
export VIDEOHUB_D_RUN=/path/to/private/acceptance-run
umask 077
mkdir -p "$VIDEOHUB_D_RUN"
uv run --env-file "$VIDEOHUB_D_ENV" alembic upgrade head
uv run --env-file "$VIDEOHUB_D_ENV" uvicorn app.main:app --host 127.0.0.1 --port 18000
```

在同一个隔离环境的另一个 Bash 终端启动六个 Worker。使用独立终端或现有进程管理方式保留 PID。压测期间保持这六个日志文件可连续读取。

```bash
for name in control media-1 media-2 asr llm render; do
  queue="${name%%-*}"
  if [ "$name" = control ]; then queue=control,celery; fi
  uv run --env-file "$VIDEOHUB_D_ENV" celery -A app.infrastructure.queue.celery_app:celery_app \
    worker --pool=solo -c 1 -Q "$queue" -n "$name@%h" --loglevel=INFO \
    > "$VIDEOHUB_D_RUN/$name.log" 2>&1 &
done
uv run --env-file "$VIDEOHUB_D_ENV" celery -A app.infrastructure.queue.celery_app:celery_app \
  inspect active_queues --timeout=5
```

开始前核对队列响应中有六个实例且队列正确。结束等待空闲后再检查 active、reserved、scheduled 和隔离库中的未终结任务。出现任务状态无法确认时，脚本保存报告并停止启动后续波次。报告中的已知任务可通过任务详情定位，并用同一个测试账号调用取消接口。还原工作环境时只停本次隔离实例。

长时和对象分发测试使用与生产一致的每分钟补发节奏。在隔离 Linux 测试终端运行下面的循环，任务结束后停止。环境文件沿用隔离配置。

```bash
while true; do
  timeout 120s uv run --env-file "$VIDEOHUB_D_ENV" python -m app.modules.billing.manage reconcile
  sleep 60
done
```

## 波次与报告

测试清单是波次数组，每波含 `users` 和 `jobs`，每项含 `id`、`account`、`cache` 和 `quote`。所有任务 ID 使用唯一的测试标签。账号文件使用 JSON 对象，键与清单的账号标签匹配，值为对应的 Bearer token。脚本会通过 `/auth/me` 校验标签映射到不同用户。

以下是一个波次的结构。补齐 1、2、5、10 人各一波。每波需有对应数量的独立账号，单账号可提交多项任务。每个 `quote` 只指定 `url`、`upload_id` 或 `transcript_id` 中的一种。

```json
{
  "users": 1,
  "jobs": [
    {
      "id": "d1-video-20m",
      "account": "u01",
      "cache": "cold",
      "delivery": "local",
      "quote": {
        "action": "download",
        "url": "<公开免登录样本 URL>",
        "preset_id": "video_720_mp4"
      }
    }
  ]
}
```

| 波次 | 负载 |
| --- | --- |
| 1 人 | 建立单用户基线，分别测冷下载、冷 ASR、已有文稿的文本 AI，再对照同来源缓存命中 |
| 2 人 | 两路不同来源的下载，以及其中一位用户并发提交已有文稿总结。检查两个 media PID 的传输时间区间与 LLM 时间区间 |
| 5 人 | 冷下载、无字幕转写、已有文稿总结或思维导图混合。加入 20/40/60 分钟样本、有字幕、MP3 及需转码来源。按同一业务配比增加波次，连续观测至少 60 分钟，并区分不同长视频与相同内容复用 |
| 10 人 | 同时发起报价、随后各自提交确认。记录受理、队列消退时间、失败、恢复及低负载复测。确认并发按实际时间观察，报价耗时会自然分散确认请求 |

三平台使用固定、公开且允许下载处理的样本。冷样本使用不同业务来源和干净缓存，命中样本重放已完成的相同请求。COS 直下测试设置 `"delivery":"direct"`，报价使用 `"delivery_preference":"server"` 生成成品。脚本等镜像成功后检查 307、签名目标和传输字节数。下载任务也可省略 `delivery`，只测生成成品。

```bash
uv run python scripts/acceptance_d.py "$VIDEOHUB_D_RUN/waves.json" --validate

uv run python scripts/acceptance_d.py "$VIDEOHUB_D_RUN/waves.json" \
  --api http://127.0.0.1:18000 --environment isolated-cloud \
  --tokens "$VIDEOHUB_D_RUN/tokens.json" --output "$VIDEOHUB_D_RUN/report.json" \
  --interface eth0 --storage /path/to/isolated-storage --baseline-seconds 60 \
  --worker-log "$VIDEOHUB_D_RUN/control.log" \
  --worker-log "$VIDEOHUB_D_RUN/media-1.log" --worker-log "$VIDEOHUB_D_RUN/media-2.log" \
  --worker-log "$VIDEOHUB_D_RUN/asr.log" --worker-log "$VIDEOHUB_D_RUN/llm.log" \
  --worker-log "$VIDEOHUB_D_RUN/render.log"
```

如果波次含 `direct`，追加 `--object-origin https://<桶名>.cos.ap-chengdu.myqcloud.com`。同地域上传比较使用固定大小样本，记录 `object_upload` 耗时和成品字节数，在云监控同时取对应区间的公网入站和出站速率。网卡统计包含云内与公网流量。最终用户的公网下载需从外部网络单独通过真实入口取样，与本机 loopback 下载分开报告。用 Range 验证鉴权及流量结算，对实际样本用 ffprobe 检查轨道和时长，再完整解码。单机指标按基线、持续负载、恢复区间对比。

- HTTP 受理 p95 从确认请求发出到 API 返回任务 ID。报价和报价加确认各有独立指标。成功请求的延迟与总请求、拒绝、超时同时呈现。
- 首个有效处理等待从服务端任务创建时间到同机实际操作的计时起点。下载从源提取或实质传输/渲染计时取证；AI 与转写从字幕、音频、ASR 或模型操作取证。缺少冷任务证据时该波次不能达标。最终结果复用和来源元数据复用分别记录。
- 各阶段 `dispatch_to_claim_ms` 从记录派发到 Worker 成功领取，覆盖消息与 Worker 等待。派发前等待会体现在端到端时间，另外通过任务等待原因排查。内部阶段可能嵌套，不能累加。CPU 忙碌、I/O wait、steal、内存、Swap、网速、磁盘可用空间随时间采样。
- 按报告中的任务 ID 对照 Worker 原始日志里的 `asr_preprocess`、供应商执行预算、超时和错误码，区分预处理与远程模型响应。停机或排空时结合 reconcile 日志核对重试和回收。
- 在 1/2/5 人中判定通过档位，要求冷任务首个有效处理 p95 ≤ 2 秒、确认 API p95 ≤ 1 秒、任务及交付通过。10 人突发用于观察积压、恢复和失败。少量样本的 p95 采用最近秩，多次波次后再确定可持续档位。`complete` 表示波次均已执行，逐波 `targets_met` 表示该波满足测量目标。

## 健康检查与本地验证

```bash
curl -fsS --max-time 5 http://127.0.0.1:18000/api/v1/health/live
curl -fsS --max-time 5 http://127.0.0.1:18000/api/v1/health/ready
curl -fsS --max-time 5 http://127.0.0.1:18000/openapi.json -o /dev/null
curl -fsS --max-time 5 http://127.0.0.1:18000/docs -o /dev/null

uv run pytest -q tests/test_uploads_celery.py -k capacity
uv run pytest
```

Swagger UI 位于 `http://127.0.0.1:18000/docs`。用测试 token 调用 `POST /api/v1/billing/quotes`、`POST /api/v1/billing/tasks`，再用返回的任务 ID 调用 `GET /api/v1/tasks/{task_id}`。容量脚本的本地集成测试使用独立 PostgreSQL schema、Redis、Solo Worker、FastAPI 与 FFmpeg，来源数据、ASR 和 LLM 使用本地替身。真实平台长视频、外部模型、对象存储和公网容量由目标机试验得出。

本地试跑中的一次采样如下。媒体替身固定耗时 1.5 秒。2 人波次验证两个媒体 Worker 进程的传输区间重叠，并包含已有文稿总结。5 人波次含一次成品复用。

| 账号数 | 完成任务 | 确认 API p95 | 冷任务首次处理等待 p95 |
| --- | --- | --- | --- |
| 1 | 1/1 | 13 ms | 94 ms |
| 2 | 3/3 | 38 ms | 172 ms |
| 5 | 6/6 | 65 ms | 3.11 秒 |
| 10 | 10/10 | 122 ms | 6.23 秒 |

## 云机短样本实测（2026-11-20）

在目标云机的私有目录运行 FastAPI 和六个 Solo Worker。独立 PostgreSQL 14.24 迁移到 0014，Redis 7.4.12 通过私有 Unix 套接字提供进度和队列。验收前后的生产 API 和 Worker PID 相同，生产 readiness 返回 200。结束后停止隔离服务。

本轮使用 5 个已登记的 B 站公开视频（106–149 秒）和 10 个独立测试账号。第一次固定提交 480p，4 项因 `PRESET_UNAVAILABLE` 失败。复测先经 `POST /api/v1/videos/parse` 获取可用档位，再在新建的隔离数据库和存储上跑 1/2/5/10 人波次。结果中的冷下载指首次生成成品，报价前的解析已预热来源元数据。AI 波次使用接近 20/40/60 分钟的生成音频、Mock ASR 和本地启发式 LLM。

| 账号数 | 完成任务 | 确认 API p95 | 冷任务首次处理等待 p95 |
| --- | --- | --- | --- |
| 1 | 1/1 | 65 ms | 0.81 秒 |
| 2 | 3/3 | 112 ms | 1.63 秒 |
| 5 | 5/5 | 196 ms | 0.96 秒 |
| 10 | 11/11 | 366 ms | 1.00 秒 |

2 人波次的两路真实媒体传输在不同进程内重叠约 105 ms。同时提交的文本总结在两项下载执行区间内完成。10 人波次含一次成品复用。5 个成品均通过完整解码，视频为 H.264，音频为 AAC。约 28 秒的采样窗口内，整机 CPU 忙碌峰值约 80%，最少可用内存约 5.1 GiB。

成品通过云机内的 loopback API 取回。公网分发吞吐、真实长视频的外部 ASR/LLM、同区域对象存储上传及 60 分钟持续混合负载仍需实测。复测使用项目对应的 Redis 7，Ubuntu 默认 Redis 6 包的 Lua 环境缺少 `cjson`，会让任务在发布进度时停留在待入队状态。

## 本次修改

- 新增 `scripts/acceptance_d.py` 与本文。
- 在 `app/infrastructure/{timing,process_runner}.py` 和 `app/modules/download/child.py` 增加任务及源传输计时。在 `app/modules/tasks/{pipeline,repository,service}.py` 记录派发至领取的时间，保持任务 API 字段。
- 扩展 `tests/test_uploads_celery.py`。D 包沿用 A/B/C 的迁移和功能开关。生产发布和目标机隔离负载试验均需先确认。
- 在 `README.md` 和 `DOWNLOAD_PERFORMANCE_PLAN.md` 增加验收入口。
