**智能切片 R2 交付（2026-11-02）**

范围：F05–F11，仅后端；保留 R1 的任务执行权、进程组退出确认、CAS 与整批发布约束。`CLIPS_ENABLED` 默认仍为 false。

- F05：先确定精确原句；同处重叠区间不再相互制造歧义，时间分离的重复台词仍需复核。近似匹配增加否定、数量与 C++/C# 等符号差异检查，保留计算预算。这是保守词法校验，不保证所有语义相同。
- F06：任务启动在账号锁内预留次数；Worker 持久化实际 cache/render 模式，晚命中缓存不耗渲染次数，已开始真实处理的失败仍计数。SQL 聚合替代整日 payload 拉取；旧数据保守兼容，不改历史账务。
- F07：确认与执行验证报价冻结的 upload_id、owner、SHA 和可用性；同内容新上传不使旧实例失效。冻结实例丢失需重新报价。
- F08：源 pin 原子替换；每次清理只构建一次保护索引。损坏 pin 通过任务的视频依赖保护相关源，保留对应进程资源，不再阻止所有用户的媒体清理。
- F09：探测分别返回完整资源、容器、音轨、视频轨、共同可剪区间与计费音频时长。完整资源用于导出权益；精确 SHA 不再因音视频轨长短不同被判变化；音频尾部没有画面时明确拒绝。
- F10：整批窗口先合并、按 WAV 上传/临时空间上限拆分并计入重复上下文；在任何模型调用前检查整批音频、请求数、时间及空间预算。
- F11：保留输入 seek 前的关键帧，以 30 fps 保持画面后精确裁切；不放宽同步阈值。真实 1/2 fps、稀疏 VFR、25/30 fps 验证第一帧内容、同步与完整解码。渲染缓存版本更新至 renderer-v2，定位版本 timing-v3。

修改清单见 `reviews/clips-r2-files.txt`。新增正式回归 `tests/test_clips_r2.py`，扩展 PostgreSQL 次数准入测试；历史错误复现改为跳过，不能把错误断言通过视为修复。

验证：`uv run pytest` 全量 **502 passed，203.08s**；R2 新增 18 条正式回归，另增 1 条 PostgreSQL 准入/模式回归。真实 FFmpeg、隔离 PostgreSQL/Redis/Celery 与 R1 的 36 条回归均通过。结果见 `reviews/clips-r2-regression.xml`。R3 复查另补了 4 条数值/单位匹配回归，最终合并结果见 `CLIPS_R3.md`。未调用付费模型、迁移业务库或重启既有服务。

启动（分别开终端，PostgreSQL/Redis 沿用项目本地配置；启用切片前配置 `clips.env.example`）：

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

Swagger：打开 `http://127.0.0.1:8000/docs`，登录后依次调用 `/api/v1/clips/plans/quotes` → `/api/v1/billing/tasks` → 查询方案 → `/api/v1/clips/exports/quotes` → `/api/v1/billing/tasks` → 任务 SSE/查询 → 查询成片 → 文件凭据与 Range 下载。

```sh
# TOKEN 为已有会话凭据；PLAN_ID 和候选 ID 来自上述方案结果。
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/clips/plans/$PLAN_ID
curl -X POST http://127.0.0.1:8000/api/v1/clips/exports/quotes \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"plan_id":"<PLAN_ID>","revision":1,"candidate_ids":["<CANDIDATE_ID>"]}'
# 将返回 quote_id 提交 /api/v1/billing/tasks，然后查询返回 task_id。
```
