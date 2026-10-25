# 视频问答、原文引用与个人知识库 · V1

当前实现覆盖当前视频的独立问答、原文片段引用、收藏 / 标签 / 文字搜索，以及总结结构改进。
沿用 FastAPI、PostgreSQL、Redis、Celery、现有 Protocol 模型适配器与额度体系，没有增加依赖或新服务。
未接支付，未调用付费模型验收。

## 用户规则与实现边界

- 先成功获取文稿 / 总结，再针对当前文稿提问。每个新问题成功消耗 1 次 AI，不重复消耗转写分钟；收藏、搜索、阅读免费。
- 问答先报价，再通过现有 `/billing/tasks` 确认。排队不冻结，启动时冻结，回答与引用在 CAS 成功事务中持久化后才核销。
- 文稿无足够相关依据、模型拒答、无效结构 / 引用、超时或有效取消均不核销。内部重试不增加用户次数。
- 问答为独立单轮，不携带先前聊天上下文。首版使用确定性的中英文词项 / 中文双字匹配，不引入向量库；同义改写、跨段推理的召回效果仍需真实模型评测。
- 文稿最多 60000 字符，问题 2–1000 字符；最多 8 段、每段 700 字符的原文进入回答上下文；一次逻辑模型调用，最多 4096 输出 Token。网络层仍有已有有限重试和总超时保护。
- 模型只能返回提供的引用编号；后端验证编号并从源文稿生成引用内容及时间，拒绝不存在的编号。引用校验保证出处对应，不等于对每个模型结论做事实核验。
- ASR 引用保留原分段范围，标“约”，不声称逐句精准定位。字幕保留原时间范围。工作台可跳到对应文稿，YouTube / B 站链接附时间参数；是否跳转仍取决于平台播放器。
- 用户、文稿 ID / 内容哈希、规范化问题、模型、问答版本和语言共同确定缓存。同账号相同版本的已成功回答免费复用，不共享其他用户私有问题或回答。
- 问答只使用已授权文稿，已有长视频文稿不因会员到期被要求重新购买下载权限；新下载继续校验当前权益。AI 次数、并发和频率限制保持生效。
- 回答列表默认显示最近 20 条；任务历史携带回答 ID / 文稿 ID，能直接读取更早的自己的回答。隐藏历史不撤销既有授权。
- 收藏按用户 + 文稿唯一，支持最多 12 个标签，每个 1–24 字；重复收藏更新标签和关联总结。移出收藏只隐藏记录，再次收藏恢复同一条，不删除视频任务或结果。
- 收藏保存文稿与总结的文字快照，媒体文件 24 小时过期不会删除收藏。查询仅在当前用户记录中执行，匹配标题 / 文稿 / 总结 / 标签，支持标签筛选与分页；`%` / `_` 按文字处理。
- 总结新增核心结论、适用人群、行动步骤、原文依据。有依据才填，不强行补齐。引用必须逐字匹配文稿，时间由后端取源分段；长文聚合保留经过校验的引用。
- 总结 / 导图默认提示词版本升级为 summary-v2 / mindmap-v2，旧数据仍兼容；如环境显式配置旧版本，部署时同步更新版本。已有结果不会被自动重写或自动扣费。

## 数据与模块边界

新增 knowledge 模块持有 `video_answers`、`saved_videos`。
`video_answers` 以 `(user_id, question_key)` 唯一、task_id 唯一；收藏以 `(user_id, transcript_id)` 唯一，并索引用户与更新时间。
通过 TranscriptService 验证文稿授权，通过 AI / Video / Task / Allowance Repository 或 Service 获取上下文及编排。
回答写入使用 TaskRepository.complete_with 的事务回调，和任务完成、额度核销一起提交；取消先赢则不写回答。
任务结算优先识别 answer_id，避免把“已有文稿”误判为“已有回答”而让新问答免费。

## 启动与迁移

需正常停止旧 API / Worker，避免旧进程缺少 knowledge.question 注册。
迁移仅新增两张表，不改写旧结果。生产迁移前按既有流程备份数据库；不要在业务库执行 downgrade。

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run alembic upgrade head
uv run alembic check
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

独立终端，保持唯一一个 Worker：

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

前端：

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/frontend
pnpm dev
```

后续也可正常停止已有服务后使用根目录 `./scripts/dev.sh --skip-install`，自动迁移并启动完整应用。
额度恢复 / 队列重派沿用 `uv run python -m app.modules.billing.manage reconcile`，部署时安排独立定时执行。

## API 与手动验收

所有下列接口位于 `/api/v1/knowledge`，均要求登录；成功包裹 data，错误保持 request_id。

| 方法 / 路径 | 用途 |
| --- | --- |
| POST /questions/quotes | transcript_id、question、language，返回报价；确认仍走 `/billing/tasks` |
| GET /answers?transcript_id=... | 当前用户此文稿最近 20 个已成功回答 |
| GET /answers/{id} | 获取自己的指定回答 |
| POST /library | transcript_id、可选对应 summary result_id、tags，幂等收藏 / 编辑 |
| GET /library | q、tag、limit、offset、可选 transcript_id，搜索自己的收藏 |
| GET /library/{id} | 收藏全文 |
| DELETE /library/{id} | 移出收藏，隐藏记录 |

在 Swagger `/docs` 登录授权后，先从自己的成功任务取得 transcript_id。

```sh
BASE=http://127.0.0.1:8000/api/v1
TOKEN='替换为自己的登录凭证'
TRANSCRIPT_ID='替换为自己的文稿UUID'
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/knowledge/library?q=缓存"
curl --max-time 8 -X POST "$BASE/knowledge/library" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"transcript_id\":\"$TRANSCRIPT_ID\",\"tags\":[\"学习\"]}"
curl --max-time 8 -X POST "$BASE/knowledge/questions/quotes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"transcript_id\":\"$TRANSCRIPT_ID\",\"question\":\"视频主要讲了什么？\",\"language\":\"zh\"}"
```

报价不执行 AI。查看 maximum 后，用 quote_id 在 `/billing/tasks` 确认；真实模型任务可能产生供应商费用。
浏览器：登录 → 获取文稿 → 文稿与总结中收藏 / 编辑标签 → 顶栏知识库搜索 → 阅读与提问。
工作台新增“视频问答”Tab；引用可查看对应文稿；关闭页面后从任务历史继续。

## 验证记录与修改清单

自动化覆盖：授权、私有缓存、报价 / 单次核销、相同回答复用、取消与失败释放、无效引用、文字检索 / 标签 / 通配字符、收藏幂等、隐藏 / 恢复、真实 PostgreSQL 并发收藏。
浏览器使用独立 PostgreSQL schema 与离线摘录样本，验证搜索、收藏阅读、问答引用、报价确认和手机布局；样本不写入业务数据，不将离线摘录当作真实模型效果。

```sh
cd backend
uv run pytest
uv run alembic check
cd ../frontend
pnpm run type-check
pnpm test:unit
pnpm run build
```

- 后端新增：`app/modules/knowledge/{__init__,models,repository,questions,service,router,worker}.py`、`alembic/versions/0007_video_questions_and_personal_library.py`、`tests/test_knowledge.py`、本文。
- 后端更新：API / 数据库模型 / Celery 注册；tasks Service 类型和 Repository 结算识别；billing Repository / Worker 路由；ai schemas / summarizer / providers(base, heuristic)；core config 默认提示词版本；README。
- 前端新增：`components/VideoQuestions.tsx`、`components/SaveToLibrary.tsx`、`views/LibraryView.tsx`、`types/knowledge.ts`、`knowledge.css`、`tests/knowledge.test.tsx`。
- 前端更新：App 路由、main 样式引入、API、api 类型、workspace Store、工作台、任务历史 / 阶段标签、额度确认、订阅说明、顶栏 / 页脚 / Icon、README。

最终验证：后端 **284 passed**（包含新增 11 项知识工作台测试，其中 1 项验证真实 PostgreSQL 并发收藏），前端 **79 passed**；类型检查、生产构建、`alembic check` 和 `git diff --check` 通过。本地数据库已升级至 `0007`。
浏览器核对了 1440px 桌面及 375px 手机布局、原文高亮跳转、已有回答零次数报价。真实模型质量和供应商账单仍需上线前使用真实样本核对。
