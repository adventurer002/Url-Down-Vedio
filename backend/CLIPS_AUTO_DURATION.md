# 智能切片：默认按语义决定时长

2026-11-02。用户要求 AI 自主判断如何切分，取消默认固定秒数。前后端同步实施，没有迁移或新增依赖。

## 当前行为

- 默认选择“AI 自动判断（推荐）”，不显示最小/最大秒数输入。用户只需选择希望保留的候选数量上限和关注方向；数量仍可少于设置值。
- 模型在连续的原文单元中选择起止，保留话题的前提、论证、故事经过和结论；短片段和数分钟片段都可入选，不为满足 30、60 或 90 秒而截断/填充内容。
- 句级定位负责把选中的原文映射到真实视频时间；FFmpeg 按核验后的语音核心区间生成成片，不以固定秒数再切一次。
- “自定义时长范围”为可选模式，支持 1–900 秒；不满足用户手动范围时拒绝该候选，不强行截断原句。
- 整批资源保护仍存在。`CLIPS_EXPORT_MAX_TOTAL_SECONDS=900` 默认允许一批成片总长最多 15 分钟，这不是单段目标时长。定位音频预算、文件大小、总超时、磁盘配额及取消机制继续生效。超限提示减少选择，保留完整原句。

## 契约、历史与缓存

`ClipStrategy` 增加 `duration_mode: "auto" | "manual"`：

```json
{"count":3,"duration_mode":"auto","min_seconds":null,"max_seconds":null,"focus":""}
```

自动模式不能同时填写非空秒数，避免确认内容含糊。旧调用仅提交 `min_seconds/max_seconds` 时按手动模式解释，原来的手动默认值与校验继续保留。读取旧方案时，缺少 `duration_mode` 也按旧手动约束处理。

模式进入方案 spec 和缓存身份，自动分析不会误取旧手动短片段方案。旧方案、已确认任务和已有成片保持原约定；旧任务缺少新批次预算时使用原 450 秒保护。重新分析旧方案时，前端草稿默认切换为自动模式，不修改历史结果。

因此，**以前已生成的短候选需要重新分析，旧文件不会自动变长**。改变模式会作废未确认的旧报价；仍须再次确认才创建分析任务。

新报价把整批成片预算写入 `allowance.clip_limits.max_export_seconds`。精确定位与生成按该快照检查；可选的首尾余量会让位于批次预算，语音核心不会被裁掉。新模式不再沿用“5 段 × 90 秒”的隐含上限。

## 验证

- 新增后端 16 条：自动/旧手动契约、冲突输入、缓存隔离、准确字幕中 120 秒完整内容入选、4/120/300/900 秒核心完整性、超过旧 450 秒的批次、预算冻结、重叠/超限保护及真实长片段生成。
- 真实 FFmpeg 测试使用隔离的 124 秒合成视频和显式模型替身，模型选择一段连续的 120 秒论述；实际生成可下载、解码通过的约 120 秒 MP4，未被裁到 60 或 90 秒。该测试不代表真实供应商选片质量。
- 前端切片专项 75 条；新增默认自动模式、可选多分钟手动范围及模式切换后的报价失效、旧方案读取与重新分析兼容。
- 前端全量 338 条、类型检查和生产构建通过。
- F1.1 / F2 真实浏览器联调 2 条通过：F1.1 明确选手动范围验证旧流程；F2 保持自动默认值、不填写秒数，验证真实出片、混合复用、源删除、下载文件校验和历史恢复。
- 后端全量 **538 passed，318.32 秒**，报告 `reviews/clips-auto-backend-regression.xml`。

报告：`reviews/clips-auto-*.xml/.log`。本批文件清单：`reviews/clips-auto-files.txt`。本地页面已检查默认自动模式，无秒数输入框；截图在忽略提交的 `.runtime/clips-auto-duration.png`。

## 启动与验证步骤

API 与 Worker 必须使用同一版代码和配置；已经启动的 Celery Worker 不会自动加载 Python 改动，需要正常重启，不能强杀正在处理的任务。既有运行任务不会因为本轮修改被强制中断。

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
CLIPS_ENABLED=true ALLOWANCES_ENABLED=true uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
# 另一个终端（转写/LLM/定位模型沿用已配置环境）：
CLIPS_ENABLED=true ALLOWANCES_ENABLED=true uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/frontend
VITE_CLIPS_ENABLED=true pnpm dev
```

Swagger：`http://127.0.0.1:8000/docs`。先报价、明确确认后执行：

```sh
BASE=http://127.0.0.1:8000/api/v1
curl --max-time 8 -X POST "$BASE/clips/plans/quotes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"transcript_id":"<TRANSCRIPT_ID>","strategy":{"count":3,"duration_mode":"auto","focus":"保留完整的论述和结论"},"language":"zh"}'
curl --max-time 8 -X POST "$BASE/billing/tasks" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"quote_id":"<QUOTE_ID>"}'
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/tasks/$TASK_ID"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/clips/plans/$PLAN_ID"
```

视频生成/下载继续按 [F2 验收步骤](../frontend/CLIPS_F2.md) 执行，导出接口不变。

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run pytest
uv run --with playwright pytest ../frontend/scripts/clips_f1_smoke.py ../frontend/scripts/clips_f2_smoke.py -q
cd ../frontend
pnpm type-check
pnpm test:unit
pnpm build
```

本轮未调用付费模型，未修改真实 `.env` 或替用户发起实际视频分析。真实模型质量与 Safari/iOS 仍需后续验收。
