**智能切片 R3 交付（2026-11-02）**

范围：O01 单片复用，以及复查发现的缓存、清理和数值匹配边界；仅后端，未重启现有服务，未迁移业务库，未调用付费模型。

实现与约束：

- 定位窗口按用户、确切源 SHA、窗口、模型/服务地址和 timing-v3 缓存；候选核心区间另按方案、候选内容及同一来源缓存。窗口仅在整批匹配成功后入库，避免失败的 ASR 文稿污染重试。
- 成片键包含用户、确切源 SHA、最终起止时间、timing-v3、输出 profile 和 renderer-v2。每次选择重新计算播放留白；邻居变化造成最终区间变化时，必须重新编码。
- 批次只聚合已有与新成片。相同最终区间的子集请求不再调用定位模型或编码；全部命中时不需要原片，原片已删除也可交付。混合批次只处理缺失项。
- 复用文件持有共享读锁直到数据库提交，并在同一事务再次检查媒体行及真实过期时间。不会延长旧文件 TTL；每项保留自己的 expires_at。取消、失败或提交前过期均不发布新批次的部分结果；提交回复丢失不重复登记。
- 任务次数按实际工作模式收敛：全部复用且没有新定位/编码时不耗渲染次数；实际模型/编码工作开始后不得改称缓存以退还次数。仅缓存准入后若文件失效，必须重新报价，不能继续下载或转码。
- 源 pin 在共享文件锁及清理 lease 内原子发布，使清理的一次保护快照不会漏掉新读者。
- 首帧采用以切点为原点的 30 fps 网格，避免普通 30 fps 视频的非整帧裁切选择下一帧；独立从源文件取预期展示帧比对，不复用渲染器的滤镜计算作为预期值。
- 补充 R2 数值匹配保护：小数、正负号、单位、分数与关键运算符不再被去标点过程吞掉；词法保护仍不能替代真实 ASR 精度验收。

迁移 `0012_shared_clip_assets.py`：移除 file_id 全局唯一约束，增加 `(export_id,file_id)` 唯一约束，以及 file_id / asset_key 索引。保留全部既有关系，file_id 继续不设媒体外键，使 TTL 清理能删除媒体而保留成片历史。存在共享引用时拒绝降级到 0011，避免丢失交付与授权关系。

历史整批缓存键保持兼容，既有成品和下载权限继续可读。历史条目缺少新粒度缓存或版本信息时，不猜测其定位和渲染版本；单片缓存会随新的定位/导出积累。

验证：最终 `uv run pytest` **522 passed，285.59s，无失败或跳过**，见 `reviews/clips-r3-regression.xml`。相对 R1 的 483 项基线，本轮新增 39 条正式回归（R2 22 条、PostgreSQL 频率 1 条、R3 15 条、PostgreSQL 迁移 1 条）。独立首帧/时间轴专项 **10 passed**。正式回归在 `tests/test_clips_r3.py` / `tests/test_clips_r3_postgres.py`；涵盖源删除、权限隔离、TTL、窗口复用、邻居留白、混合失败、取消、提交回复丢失和真实 PostgreSQL 迁移。历史独立审计为 **3 passed / 15 skipped**；15 个错误复现已由正确行为回归替代，不能把 skipped 当作运行成功。OpenAPI 路由、Celery 注册及 Python 编译均已检查。

修改清单：`reviews/clips-r3-files.txt`；R2/R3 总清单：`reviews/clips-r2-r3-files.txt`。

启用与启动（在 backend 目录，先按项目配置启动 PostgreSQL/Redis；业务环境需由维护者执行迁移）：

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run alembic upgrade head
uv run alembic check
# 独立终端启动 API 与 Solo Worker
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

保持 `CLIPS_ENABLED` 默认关闭；需要试运行时按 `clips.env.example` 配置模型和超时，再启用开关。

Swagger 在 `http://127.0.0.1:8000/docs`。先按 `CLIPS_V1.md` 创建并完成一个包含两条候选的导出，再只选择其中一条重新报价、确认并查询成片。最终区间不变时应看到相同 file_id / expires_at、timing_usage.audio_seconds=0；新任务 cache_hit=true。原片删除后重复此步骤也应成功；若删除相邻候选使播放留白发生变化，则需要可用原片生成新的最终区间。

```sh
BASE=http://127.0.0.1:8000/api/v1
# TOKEN / PLAN_ID / CANDIDATE_ID 为真实登录及方案结果。
curl --max-time 8 -X POST "$BASE/clips/exports/quotes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"plan_id":"<PLAN_ID>","revision":1,"candidate_ids":["<CANDIDATE_ID>"]}'
curl --max-time 8 -X POST "$BASE/billing/tasks" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"quote_id":"<QUOTE_ID>"}'
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/tasks/$TASK_ID"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/clips/exports/$EXPORT_ID"
curl --max-time 5 -X POST -H "Authorization: Bearer $TOKEN" "$BASE/files/$FILE_ID/download/access"
curl --max-time 10 -H "Authorization: Bearer $TOKEN" -H 'Range: bytes=0-99' "$BASE/files/$FILE_ID/download"
```

重跑验证：

```sh
uv run pytest
uv run pytest tests/test_clips_r2.py tests/test_clips_r3.py tests/test_clips_r3_postgres.py
PYTHONPATH=.:tests uv run pytest reviews/reproduce_clips_findings.py -q
```

审查收敛：首轮核查缓存键/来源/关联迁移；第二轮发现并修正失败窗口污染、清理快照与 pin 发布竞态、小数及符号被吞掉；第三轮用独立预期画面和故障注入发现并修正 30 fps 首帧偏差与缓存准入后失效分支；第四轮核对账号锁、取消/CAS、提交重试、权限、TTL、源隔离与兼容性，未再确认新的阻断问题。结论只覆盖本次代码和测试范围，不是对所有供应商和媒体格式的无缺陷保证。
