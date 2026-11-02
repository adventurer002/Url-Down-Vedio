**智能切片 R1 · 进程、任务与剪辑边界修复**

日期：2026-11-02。范围为 `CLIPS_REVIEW_AND_FIX_PLAN.md` 的 F01–F04。R2 的匹配、频率核销、来源实例、时长口径、预算预检、损坏源保护标记及低帧率问题，以及 R3 的按片段复用，仍待后续批次。

**本批行为**

- 外部进程在任务根目录 `.operations/{operation_id}.json` 登记，不再仅保存最后一个 PID。登记包括主机、任务、工作子目录、执行标识及 attempts；状态先写 starting，启动后原子更新 running，确认整个进程组退出后更新 exited。
- 清理、预留容量释放、上传删除与 reconcile 共用有界的进程检查，兼容旧根目录及嵌套 `.process.json`。所有组退出才可回收；损坏标记、未知启动结果、异常 PID、不可读目录及扫描越界均保留相关资源。已确认退出的记录不会因旧 PGID 被复用而误认成当前读者。
- 定位音频和 Qwen 临时分段目录在 PROCESS_CLEANUP_FAILED 时保留。普通错误或正常结束且没有进程读写时仍及时清理。媒体提交清单的未登记文件也受到同一保护，不能先删成品再检查进程。
- 切片任务用本机执行锁及唯一执行标识保护启动到收尾。启动 CAS、启动后读取及预算装配都在受保护入口内；失败收尾仅能更新本次执行，不能覆盖别的执行者或已经完成的任务。
- 执行标识保存在既有任务 JSON 的内部字段 `_execution_id`，不改变报价业务参数、不新增数据库列，任务 API 会移除此字段。`.attempts/` 中的原子凭据及 OS 执行锁帮助独立 reconcile 判断某次尝试是否已经停止，即使同一个 Solo Worker PID 仍存活，也可安全收敛数据库故障留下的运行状态。原有任务缺少新凭据时仍走保守的死亡 Worker 恢复分支。
- 数据库回复不明时先核对、条件收尾；持续不可用则保留凭据，恢复后由 reconcile 处理。成功、失败、取消和额度释放仍由 PostgreSQL 事务及 CAS 决定，不依赖 revoke(terminate=True)、TTL 或仅凭 Worker PID。
- 精细定位先获得不可截断的语音核心区间，再分配可选前后留白。留白受源边界、相邻核心和时长剩余额度约束：相邻核心不会被留白扩成重叠，90 秒核心不再被扩成超长。真正核心重叠、核心超长仍拒绝。
- 派生定位及新导出缓存版本升级到 timing-v2；旧已交付结果继续可读，使用新算法的请求不会误命中旧定位结果。没有重写或删除历史结果。

回归中同时完成两项兼容调整：进程控制文件不计入 native Watchdog 的媒体负载体积，但仍完整计入全局存储配额；日计数查询将北京时间日界转换为同一 UTC 瞬间，保证 PostgreSQL 与 SQLite 在跨日时一致。后者不改变 R2 待修的“执行时缓存命中被计为渲染”规则。

**启动与运行**

本批无新增依赖、配置或迁移。已有环境仍需先应用切片阶段 A 的 `0011`；本轮没有迁移业务库、重启已有服务或调用付费模型。

API、Worker、定时 reconcile/cleanup 入口应使用同一版代码。更新时温和停止旧 Worker，确认其当前任务和外部进程结束，再启动新版本。

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

另一个终端：

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

独立恢复/清理命令保持不变：

```sh
uv run python -m app.modules.billing.manage reconcile
uv run python -m app.infrastructure.storage.cleanup
```

`CLIPS_ENABLED` 默认关闭状态不变，定位模型配置沿用 `clips.env.example`。运行数据新增 `.operations` 和 `.attempts` 控制记录，需保留到安全恢复或清理结束。

**Swagger 与 curl 验收**

在 `http://127.0.0.1:8000/docs` 登录后，仍走 clips 分组报价与 `/billing/tasks` 确认流程；本批没有新增 HTTP 路径。完整视频来源、报价、SSE、Range 步骤见 `CLIPS_V1.md`。

```sh
BASE=http://127.0.0.1:8000/api/v1
TOKEN='替换为自己的登录凭证'
TRANSCRIPT_ID='替换为自己的文稿UUID'
curl --max-time 5 "$BASE/health/ready"
curl --max-time 8 -X POST "$BASE/clips/plans/quotes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"transcript_id\":\"$TRANSCRIPT_ID\",\"strategy\":{\"count\":3,\"min_seconds\":30,\"max_seconds\":90},\"language\":\"zh\"}"

QUOTE_ID='替换为报价UUID'
curl --max-time 8 -X POST "$BASE/billing/tasks" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"quote_id\":\"$QUOTE_ID\"}"

TASK_ID='替换为任务UUID'
curl -N --max-time 310 -H "Authorization: Bearer $TOKEN" "$BASE/tasks/$TASK_ID/events"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/tasks/$TASK_ID"
```

取候选创建导出报价并确认后，查询 `/clips/exports/{id}` 和每条 file_id 的下载接口。核对相邻语音核心不会仅因留白而失败，90 秒核心也不会被强行截短。取消使用 `POST /tasks/{id}/cancel`，等待终态后核对额度、原片与临时目录。数据库异常和仍存活进程组的场景由下列隔离自动化用例验证。

**测试与审计记录**

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run pytest tests/test_clips_r1.py -q
uv run pytest
uv run python -m compileall -q app
PYTHONPATH=.:tests uv run pytest reviews/reproduce_clips_findings.py -q \
  --junitxml=reviews/clips-audit-r1-results.xml
```

`test_clips_r1.py` 为期望正确行为的正式测试，涵盖启动后读取异常、启动回复丢失、错误执行者隔离、数据库持续故障后的活 PID 恢复、取消与重复恢复、并发重投、真实多进程组、旧嵌套标记、未知退出、容量/提交文件保护、普通临时目录清理、合法区间与真实非法区间、控制文件计量和跨日查询。

原审计 A01/A02/A04/A09/A10/A15 已由正式测试取代，在历史错误复现脚本中标记跳过；剩余用例仍用于复现 R2/R3 已知问题。原 `clips-audit-results.xml` 保留为修复前证据，新结果另存 `clips-audit-r1-results.xml`。该脚本通过不等于剩余缺陷已修复。

**修改文件清单**

新增：

```text
backend/app/infrastructure/process_control.py
backend/app/modules/tasks/execution.py
backend/tests/test_clips_r1.py
backend/CLIPS_R1.md
backend/reviews/clips-audit-r1-results.xml
```

修改：

```text
backend/app/infrastructure/process_runner.py
backend/app/infrastructure/storage/local.py
backend/app/infrastructure/storage/cleanup.py
backend/app/modules/billing/manage.py
backend/app/modules/clips/service.py
backend/app/modules/clips/timing.py
backend/app/modules/clips/worker.py
backend/app/modules/tasks/repository.py
backend/app/modules/tasks/service.py
backend/app/modules/transcript/providers.py
backend/app/modules/uploads/service.py
backend/reviews/reproduce_clips_findings.py
backend/CLIPS_REVIEW_AND_FIX_PLAN.md
backend/CLIPS_V1.md
backend/README.md
```

原有其他工作区改动保持不变；frontend、pyproject.toml、uv.lock、环境配置模板和迁移文件均未修改。

**最终验证结果（2026-11-02）**

- `uv run pytest`：**483 passed，177.45 秒**；相比 R1 前新增 36 项正式测试。
- 定向检查下载、切片与 R1：77 项通过（补充控制文件异常用例前的检查）；最终全量已包含后来新增用例。
- 历史审计复查：12 passed、6 skipped；6 条已修复历史复现对应正式正向测试，9 条未修复的 R2/R3 复现与 3 条对照仍通过。此结果不表示 R2/R3 已完成。
- `uv run python -m compileall -q app`、4 个切片 OpenAPI 路径、`clips.execute` 注册、`git diff --check -- backend` 及新增文件空白检查通过。
- 真实 FFmpeg/ffprobe、PostgreSQL 临时 schema、Redis 和 HTTP + Celery Solo 集成仍在全量回归内通过；ASR/LLM 使用显式测试适配器，没有调用付费模型。测试创建的临时服务与进程均已退出。
- 本批 20 个后端文件的改动均列于上文；内容指纹确认前端、依赖锁及迁移文件不变。业务库未迁移，已有服务未重启。
