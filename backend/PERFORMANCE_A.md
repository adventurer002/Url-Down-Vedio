# 性能方案 A 包：长视频超时预算

日期：2026-11-18。状态：本地代码交付及回归完成，生产未部署。面向轻量云 4 核 / 8GB / 10Mbps。本包只修改后端超时执行、诊断与配置模板，不调整 Worker 数量。

## 本包行为

- 总结/思维导图使用 AI 整体预算，获取文稿完成后才开始计算独立 LLM 阶段预算。视频问答也使用相同的截止时间上下文。
- 子调用继承父级剩余时间；父级先到期报告 AI_TIMEOUT，模型阶段先到期报告 LLM_STAGE_TIMEOUT，单次模型请求先到期报告 LLM_TIMEOUT。
- ASR 总阶段与单次请求分开。千问全部分片可以使用总阶段预算，每片请求仍有独立上限。
- 音频切片移除固定 30 秒上限，使用独立的预处理预算，失败报告 AUDIO_PREPROCESS_TIMEOUT。仍使用独立进程组和原有 Watchdog；取消/超时先等待进程组退出再删除目录。
- 云接口连接、读取、上传限时统一配置；DNS 与 TCP 连接共享连接预算，避免 DNS 用掉时间后连接重新获得完整预算。
- 同一次调用的重试和退避共用原截止时间，不重新获得完整请求预算。既有最多两次尝试保持不变。
- 新增日志包含阶段、任务 ID、实际耗时、有效预算与错误码，不包含 URL、提示词、音频、文稿或密钥。

新的截止时间工具只负责预算继承和日志，没有增加重试层、熔断层或新的服务。

## 配置与已有环境兼容

| 配置 | 新默认值（秒） | 含义 |
| --- | --- | --- |
| ASR_TOTAL_TIMEOUT_SECONDS | 3600 | 完整 ASR 阶段，包括千问切片和所有转写分片 |
| ASR_REQUEST_TIMEOUT_SECONDS | 180 | 一次 ASR 请求及其重试总时间 |
| ASR_PREPROCESS_TIMEOUT_SECONDS | 180 | 千问 FFmpeg 音频切片 |
| LLM_TIMEOUT_SECONDS | 180 | 一次模型请求及其重试总时间 |
| LLM_STAGE_TIMEOUT_SECONDS | 1800 | 文稿准备后的总结/思维导图阶段 |
| AI_TASK_TIMEOUT_SECONDS | 7200 | AI 整体执行上限，子阶段不能延长它 |
| CLOUD_CONNECT_TIMEOUT_SECONDS | 5 | DNS 与连接合计上限 |
| CLOUD_READ_TIMEOUT_SECONDS | 120 | 一次读取的无数据等待上限 |
| CLOUD_WRITE_TIMEOUT_SECONDS | 120 | 一次网络写入的等待上限，仍受请求总预算限制 |

这些是本包固定配置的有界起点。按时长/片数规划动态预算和跨阶段恢复在 B 包实现，本包不宣称已经支持断点续跑或多 Worker 并行。

已有显式配置继续优先，代码不会覆盖私有 .env 或服务器 EnvironmentFile。例如本地根 .env 的 ASR_TOTAL_TIMEOUT_SECONDS=1800 仍然生效；若服务器显式保留 AI_TASK_TIMEOUT_SECONDS=900，必须在发布时调整后重启进程，不能只替换代码。

读取优先级为：进程环境 > 根目录 .env > backend/.env > 代码默认值。新增 backend/.env.example 与 backend/.env.docker.example，分别保留 localhost 和 postgres/redis 网络示例；生产模板为 deploy/production.env.example。根目录已有模板未修改，阶段内修改均限定于 backend/。

仅复制所需配置到私有环境文件，保留已有服务地址和密钥，不用示例文件整体覆盖生产配置。无需新增依赖或数据库迁移。

## 配置诊断

在 backend/ 中执行，不发起云模型请求；不带任务 ID 时也不查询数据库：

```sh
uv run python -m app.modules.tasks.diagnostics
```

读取一条指定任务的状态、耗时和错误码，通过 TaskRepository 只读查询：

```sh
uv run python -m app.modules.tasks.diagnostics --task-id '<任务 UUID>'
```

输出只包含白名单超时配置与指定任务的非内容字段，不打印数据库连接串、密钥、输入 URL、原文或错误详情。运行时配置要在服务器正式账号相同的环境中检查，开发机输出不能代表线上。

## 启动与 Swagger/curl 验证

启动前准备现有 PostgreSQL/Redis 和有效配置，本包维持一个 Solo Worker：

```sh
cd backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另一终端在 backend/：

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=INFO
```

```sh
curl --max-time 5 http://127.0.0.1:8000/api/v1/health/live
curl --max-time 5 http://127.0.0.1:8000/api/v1/health/ready
curl --max-time 5 http://127.0.0.1:8000/openapi.json
```

打开 http://127.0.0.1:8000/docs，用现有测试账号授权。沿用 POST /api/v1/billing/quotes → POST /api/v1/billing/tasks 创建已有文稿的总结或思维导图；查询 GET /api/v1/tasks/{task_id} 或订阅其 events。无字幕视频验证 ASR 时需已有有效云模型配置及相应额度。

```sh
curl --max-time 5 -H "Authorization: Bearer $VIDEOHUB_TEST_TOKEN" \
  "http://127.0.0.1:8000/api/v1/tasks/$VIDEOHUB_TASK_ID"
```

在日志检查 performance stage=ai_task / llm_stage / asr / asr_preprocess / asr_request / llm_request，比较 budget_ms 与 duration_ms。父级到期时子阶段日志可能显示 cancelled，最终错误码由最先到期的父阶段报告。SSE 单连接结束不表示处理失败，继续查询任务状态。

真实长视频与付费模型验收未在本包本地回归中执行；生产故障仍需一个实际失败任务 ID 和正式环境配置确认。

## 修改文件

- app/core/config.py
- app/infrastructure/timeouts.py（新增）
- app/infrastructure/http_client.py
- app/modules/ai/providers/openai_compatible.py
- app/modules/ai/worker.py
- app/modules/transcript/providers.py
- app/modules/transcript/worker.py
- app/modules/knowledge/worker.py
- app/modules/tasks/diagnostics.py（新增）
- .env.example、.env.docker.example（backend/ 下新增）
- deploy/production.env.example
- tests/test_timeout_budgets.py（新增）、tests/test_ai_module.py
- README.md、PERFORMANCE_A.md、DOWNLOAD_PERFORMANCE_PLAN.md
- reviews/performance-a-tests.xml（全量测试报告）

## 验证结果

最终全量回归 **850 passed，320.30 秒**，相对原有 833 项增加 17 项回归。报告见 [reviews/performance-a-tests.xml](reviews/performance-a-tests.xml)。覆盖父级/子级超时归属、配置读取与剩余连接预算、重试不重置预算、ASR 多片预算、预处理取消清理、真实父子进程退出、LLM 阶段独立计时以及已准备文稿保留。本包命令为：

```sh
uv run pytest
```

实际受限环境执行命令：

```sh
UV_CACHE_DIR=/private/tmp/videohub-ai-uv-cache uv run --offline pytest -q --tb=short --junitxml=reviews/performance-a-tests.xml
```

临时本地 API 的 live、ready、/docs、/openapi.json 均返回 HTTP 200；ready 中 PostgreSQL 与 Redis 均为 ready，临时 API 已退出。诊断命令实测显示：本地私有配置的 ASR_TOTAL_TIMEOUT_SECONDS=1800 保持生效，ASR/LLM 单请求 180、LLM 阶段 1800、AI 整体 7200、读取/写入 120 秒。git diff --check 通过。没有启动生产 Worker、执行真实长视频任务或调用付费模型。

若受限执行环境无法写默认 uv 缓存，可使用 UV_CACHE_DIR=/private/tmp/videohub-ai-uv-cache。本地集成测试需要启动隔离 Redis Unix socket 与回环 HTTP 服务；沙箱禁止绑定时需要允许测试在沙箱外运行，不能把此环境失败当作应用回归。

## 后续范围

现有同步解析接口的 30 秒解析/35 秒请求预算、抖音浏览器取源预算、下载 600 秒预算、智能剪辑独立报价预算和文件大小/时长权益不在本包修改。慢解析异步入口、下载与转码拆阶段、多用户并发、ASR/LLM 检查点续跑、按工作量预算、对象存储分发按 B/C/D 包实施。
