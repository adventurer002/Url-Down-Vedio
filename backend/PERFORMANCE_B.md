# 性能方案 B 包 · 分阶段执行与恢复

2026-11-18。本地部署基线为轻量云 4 核、8GB、10Mbps。生产启用需要用户确认。

## 行为与范围

| 流程 | 执行顺序 |
| --- | --- |
| 视频下载 | media 取源与传输 → render 封装或转码 → 原下载接口提供成品 |
| 转写 | media 取字幕或准备音频 → 没有字幕时由 asr 转写 → 登记文稿 |
| 总结、思维导图 | 需要时按上述流程准备文稿 → llm 生成 → 登记结果 |
| 已有文稿、问答 | llm 处理 |
| 上传探测、剪辑导出 | 分别交给 control 和 render |

队列交接沿用同一个用户任务 ID、SSE 和结算记录。数据库提交下一阶段后发送 Celery 消息。现有 reconcile 定时任务补发待执行阶段，确认同机执行锁和子进程组已经释放后可恢复中断阶段一次。等待阶段可以取消，已启动阶段沿用协作取消与进程组 Watchdog。阶段恢复使用原整体截止时间。

下载按视频身份、档位、原画幅流选择和处理版本复用未过期成品，并检查现有文件与用户尺寸上限。发布和回收成品共用存储提交锁和文件清单。已有文稿和 AI 最终结果继续通过原有缓存契约复用。下载缓存适用于启用新流程后登记的成品。

同用户、同内容和处理参数的 ASR/LLM 分片结果保存 24 小时。Worker 中断恢复或用户重新提交时可以读取成功分片，再处理缺失分片。Qwen 和长音频的 OpenAI 兼容转写均支持分片结果复用。未收到供应商响应的请求存在计费和完成状态不确定性，用户任务的额度由现有数据库结算事务保证只结算一次。

默认使用两个媒体槽和各一个控制、ASR、LLM、渲染槽。免费用户下载并发启用后为 2，会员仍为 3，同用户 AI 类型并发启用后为 2。实际并发受机器、源站和模型配额限制。

目前 ASR 分片及 LLM Map/Reduce 在各自阶段内执行和保存，长任务仍能占用一个对应队列的槽位。已有同步解析入口继续承担新 URL 元数据解析，control 队列承接上传探测和兼容任务。AI 剪辑分析沿用原有内部流程。源地址共享缓存、降低容量扫描频率、对象存储、真实 1/2/5/10 用户压测留在后续工作包。

## 后端启动与验证

在隔离开发环境准备 PostgreSQL、Redis 和 FFmpeg，然后在 backend 下执行。

```sh
uv run alembic upgrade head
STAGED_TASKS_ENABLED=true uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

每个 Worker 在独立终端运行。M1 开发环境可先用一个 media 槽。验证并行下载时再启动第二个。

```sh
STAGED_TASKS_ENABLED=true uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 -Q control,celery -n control@%h --loglevel=INFO
STAGED_TASKS_ENABLED=true uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 -Q media -n media-1@%h --loglevel=INFO
STAGED_TASKS_ENABLED=true uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 -Q media -n media-2@%h --loglevel=INFO
STAGED_TASKS_ENABLED=true ASR_CHUNK_CONCURRENCY=1 uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 -Q asr -n asr@%h --loglevel=INFO
STAGED_TASKS_ENABLED=true uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 -Q llm -n llm@%h --loglevel=INFO
STAGED_TASKS_ENABLED=true uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 -Q render -n render@%h --loglevel=INFO
```

```sh
curl --max-time 5 http://127.0.0.1:8000/api/v1/health/ready
curl --max-time 5 http://127.0.0.1:8000/openapi.json
curl --max-time 5 -H "Authorization: Bearer $VIDEOHUB_TEST_TOKEN" \
  "http://127.0.0.1:8000/api/v1/tasks/$VIDEOHUB_TASK_ID"
curl --max-time 10 -N -H "Authorization: Bearer $VIDEOHUB_TEST_TOKEN" \
  "http://127.0.0.1:8000/api/v1/tasks/$VIDEOHUB_TASK_ID/events"
```

打开 http://127.0.0.1:8000/docs，用测试账号依次调用 POST /api/v1/billing/quotes 和 POST /api/v1/billing/tasks。用已有文稿提交总结，同时提交两个不同的公开视频下载，观察 Worker 日志和任务状态。对已完成视频再提交同一档位，可验证成品复用。用隔离环境中的 STAGED_TASKS_ENABLED=true uv run python -m app.modules.billing.manage reconcile 验证交接恢复。

## 已准备的部署变更

迁移版本为 0016，保持原有任务和成品记录并增加可空的阶段和下载缓存字段。STAGED_TASKS_ENABLED 默认 false。正式迁移、启用开关、重启服务和真实外部样本验证均等待用户确认。

确认后先备份 PostgreSQL，按现有流程准备并验证后端版本，停写后应用 0016。安装 deploy/videohub-phase@.service，待 legacy Worker 停止并让六个实例使用同一套配置。实例名为 control、media-1、media-2、asr、llm、render。控制 Worker 同时消费默认 celery 队列。已有 reconcile timer 继续工作。先通过 readiness 与隔离测试账号验证，再启用流量。

现有 deploy_release.py 只负责旧 Worker。启用阶段模式后，未来使用该脚本前需纳入阶段 Worker 的停写与恢复，防止运行中的进程跨版本访问数据库。初次启用可以在默认关闭开关时用原发布流程先安装后端，再在经确认的维护窗口切换实例。

回退时先关闭新任务的阶段开关，保持阶段消费者运行到队列与活跃任务排空，再恢复旧 Worker 运行方式。默认保留向后兼容的 0016 数据库结构。0016 在有活跃阶段任务时拒绝降级。生产未执行部署命令。

## 修改文件

- 数据库与任务为 `alembic/versions/0016_task_pipeline.py`、`app/modules/tasks/{models,repository,service,dispatch,pipeline,checkpoints,diagnostics}.py`。
- 下载与存储为 `app/modules/download/{adapter,child,worker}.py`、`app/modules/media/{models,repository,worker}.py`、`app/infrastructure/storage/cleanup.py`。
- 转写与 AI 为 `app/modules/transcript/{providers,service,worker}.py`、`app/modules/ai/worker.py`、`app/modules/ai/providers/checkpointed.py`。
- 队列和权益为 `app/modules/billing/{manage,policy,repository,service}.py`、`app/modules/uploads/worker.py`、`app/infrastructure/queue/celery_app.py`。
- 配置为 `app/core/config.py`、`.env.example`、`.env.docker.example`、`deploy/production.env.example`、`deploy/videohub-phase@.service`。
- 验证与交付为 `tests/test_task_pipeline.py`、`tests/test_uploads_celery.py`、`tests/test_clips_r3_postgres.py`、`reviews/performance-b-tests.xml`、`README.md`、`DOWNLOAD_PERFORMANCE_PLAN.md`、`PERFORMANCE_B.md`。

## 验证记录

最终运行 `uv run --offline pytest`，**859 项全部通过**，耗时 326.33 秒。专项覆盖两个下载与文本阶段可同时进入执行、真实 FFmpeg 渲染交接、成品复用、重复消息、额度结算与恢复、两类 ASR 的分片检查点、LLM 提示词版本隔离，以及 PostgreSQL/Redis/FastAPI 与独立 Solo Worker 的真实交接。云端模型请求在测试中使用受控替身。完整结果见 [测试报告](reviews/performance-b-tests.xml)。

启用阶段开关的临时 API 通过 curl 验证，live、ready、/docs、/openapi.json 均返回 200。六个 systemd 实例的队列与节点参数通过本地 shell 解析验证。git diff --check 通过。
