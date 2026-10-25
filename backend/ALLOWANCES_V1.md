# 额度制首版：会员、转写分钟和 AI 次数

本阶段仅修改 backend/，不接真实支付，不修改前端，不建设积分钱包。公开售价为首版产品标准，
当前只能由操作人通过服务端命令发放权益；此命令不代表支付已核验，不向浏览器提供发放接口。

## 首版标准

| 项目 | 规则 |
| --- | --- |
| 体验 | 首次关注并登录绑定后自动发 30 分钟转写、5 次 AI；30 天有效，不重复领取 |
| 月会员 | ¥19.90，每账期 300 分钟、100 次 AI；不结转 |
| 转写包 | ¥9.90 / 120 分钟，发放起 30 天有效 |
| AI 包 | ¥9.90 / 100 次，发放起 30 天有效 |
| 免费下载 | 北京时间每天成功 2 次，启动占用、成功核销、失败释放；跨午夜按启动日归属 |
| 下载并发额度 | 免费 1、会员 3，其余排队；当前 Solo Worker 和全程存储锁实际串行 |
| 视频时长 | 免费最多 20 分钟，会员最多 60 分钟；拒绝未知时长和直播 |
| 单文件 | 免费最多 256 MiB、会员最多 512 MiB；临时目录同时受全局体积限制 |
| 每日下行 | 免费 1 GiB、会员 5 GiB；重复取回和 Range 均按实际发送字节统计 |
| 生成规模 | 文稿最多 60000 字符；每调用输出最多 4096 Token；一次操作最多 16 调用、累计 16384 输出 Token |
| 防滥用 | 每账号最多 10 个未终结任务；每小时最多 60 个报价和 60 个新任务，取消不退此频率额度 |
| 文件保留 | 24 小时；同任务重复取回不再计下载次数；过期重新下载作为新任务 |
| 文件分发超时 | 单次 HTTP 下载最长 600 秒，可用单段 Range 续传 |
| 退款 | 正常交付后不支持无理由退款；支付异常及依法应退款的情形另行处理 |

转写内部以整数秒记账：整段真实音频时长向上取整一次，不对每个切片或重试重复取整。
例如 60.001 秒扣 61 秒，即 1 分 1 秒，不是 2 分钟。通过受 Watchdog 保护的本地 ffprobe 测量，
不能用最后一句字幕的结束时间代替。实际时长超过确认上限时，用户最多核销确认上限，差额记为平台承担；
超过任务的视频资源上限则终止，失败零核销。

字幕和转写缓存不扣分钟；公共 AI 缓存首次获取扣一次。自己的已成功结果查看、交互和导出免费。
总结+导图使用 ai_mindmap 操作，整体成功仅扣一次，并同时授予总结、导图和源文稿权限。
中途失败即使留下总结缓存，也不作为已购买交付。翻译、问答本阶段未新增。

先占用最早到期的额度，同到期时体验额度优先。排队不占额度；执行前原子占用本次报价的最高额度，
余额不足显示 waiting_for_allowance；补充后重试。字幕/缓存未使用的分钟在成功结算时释放。
过期批次不能再分配；已经开始的任务可使用原占用完成，释放仍保留原有效期，过期部分不复活。
取关保留账号、订单查询条件、已获得结果和付费额度，停用体验额度及免费新下载。
首版不提供自助解绑换绑；永久身份指纹和领取标记不会因重新关注而清空。

## 启动与配置

依赖沿用现有 pyproject.toml / uv.lock，没有新增依赖。配置补充见 allowances.env.example。
已有 API/Worker 需要停止旧版本后重启；无需前端构建。迁移前按现有运维流程备份数据库。

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run alembic upgrade head
uv run alembic check
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

另一个终端：

```sh
cd /Users/edgar/Desktop/项目/videohub_ai/backend
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

ALLOWANCES_ENABLED 默认 true；启用时新耗时任务必须登录，并通过下述报价确认接口创建。
旧创建接口没有 quote_id 时返回 QUOTE_REQUIRED，不能绕过额度。现有前端尚未接入此新流程，
本阶段使用 Swagger / curl 验收。ALLOWANCES_ENABLED=false 仅用于开发环境旧接口兼容，
不要在商业部署中关闭，更不要通过关闭开关清除已有占用。旧匿名任务不会自动归属新用户。

当前代码按真实模型配置运行；离线验收应在 API 和 Worker 同时设置 LLM_PROVIDER=heuristic、ASR_PROVIDER=mock，
这些结果是明确的模拟/摘录，不代表真实模型效果，生产收费路径拒绝这些模拟能力。
开启额度时，生产环境也拒绝模拟公众号登录与身份发放。

## Swagger / curl

打开 http://127.0.0.1:8000/docs，先按 STAGE9.md 的公众号流程登录，或使用已有 Bearer Token。

```sh
BASE=http://127.0.0.1:8000/api/v1
TOKEN='替换为登录凭证'
curl --max-time 5 "$BASE/billing/catalog"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/billing/allowances"
curl --max-time 40 -X POST "$BASE/billing/quotes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.bilibili.com/video/BV1g2JX6nESP","action":"ai_mindmap","language":"zh"}'
```

查看 maximum 中最高转写秒数和次数后，用返回的 quote_id 确认；报价 15 分钟有效。

```sh
QUOTE_ID='替换为报价ID'
curl --max-time 5 -X POST "$BASE/billing/tasks" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"quote_id\":\"$QUOTE_ID\"}"
TASK_ID='替换为任务ID'
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/tasks/$TASK_ID"
curl -N --max-time 310 -H "Authorization: Bearer $TOKEN" "$BASE/tasks/$TASK_ID/events"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/billing/ledger"
```

重复确认同一报价返回同一个任务。失败后重试需新报价。取消沿用 POST /tasks/{id}/cancel。
已确认报价不受后续价格更改影响，模型、提示词版本、额度上限和生成规模随任务保存。
排队跨会员到期时重新检查当前权益，已开始的任务使用执行时权益。

服务端发放（user-id 从登录后的 GET /auth/me 获取）：

```sh
uv run python -m app.modules.billing.manage grant \
  --user-id '替换为用户UUID' --product monthly --key 'manual-membership-001' --actor 'operator-name'
```

product 还可用 asr_120、ai_100。key 是必填幂等键，重复执行不重复发放，不同请求复用同键拒绝。
提前续费的下一期额度从下一账期才可用，月末锚点保持原日期，例如 1/31→2 月末→3/31。
没有客户端支付成功发放接口，没有真实支付回调或现金退款接口。

## 恢复与检查

```sh
uv run python -m app.modules.billing.manage reconcile
uv run pytest
```

部署时应独立于 Solo 长任务周期执行 reconcile（建议每分钟一次，单次扫描有界）；不新增 Redis 之外的消息服务。
排队任务作为持久派发表，派发 ACK 不明时保留原状态，通过重投与启动 CAS 恢复。
定期重投按最近派发时间轮转，避免余额不足的旧任务长期挡住其他账号。
补发或购买后会尝试唤醒本账号排队任务，也可 POST /billing/resume。

进程恢复只处理同主机、确认原 Worker PID 已不存在且子进程组也已退出的任务，然后清理临时目录并释放额度。
活 Worker、存活孤儿进程组、PID/主机或进程标记无法确认时保留占用，需运维核实，不能按 TTL 盲目退款。
任务未标记成功的孤立成品仍由现有存储清理流程回收。
HTTP 进程崩溃无法报告最终发送量时，一小时后按该请求预留量保守计入当日流量，不将其当作现金消费。

账务以 PostgreSQL 为准，Redis 仅承担事件和通知。终态与核销在同一事务，晚到取消不能覆盖完成，
失败释放不可重复；隐藏历史不影响额度或已有结果授权。流水记录发放、占用、实际时长、核销、释放和到期。
新增专项测试包含实际 ffprobe、真实 PostgreSQL 独立 schema 的并发竞争，不调用付费模型或支付接口。

回退优先回滚应用并保留新增表；0006 的 downgrade 会删除额度与流水表，应先停任务、导出账务并备份。
迁移升降级验证只在独立测试 schema 中进行，不对现有业务账务执行删除。

## 修改范围

- 新增 modules/billing/：规则、表模型、Repository、Service、Router、Celery 入口、管理和恢复命令、流量响应。
- 新增 0006 迁移、media/probe.py、ai/providers/bounded.py、tests/test_allowances*.py、测试环境隔离配置。
- 修改 tasks 的模型、Repository 与 Service，接入报价、占用与终态事务；auth 只通过 Repository 契约提供身份。
- 修改下载、转写、AI Worker，增加时长/规模限制、组合交付、文件流量；复用现有 Watchdog、SSRF、SSE。
- 修改 API Router、Celery 注册、数据库模型注册、后端配置，以及 backend/README.md 的入口说明。

## 本次验证（2026-10-24）

- `uv run pytest -q`：273 passed（含 31 项额度专项测试，其中 3 项使用真实 PostgreSQL）。
- 本地数据库迁移至 `0006`；`uv run alembic check` 无差异。
- 独立测试 schema 完成 upgrade head → downgrade 0005 → upgrade head → check，全通过。
- 临时 API 的 curl 验证：ready 200、catalog 200、未登录 allowances 401；测试服务已停止。
- `git diff --check` 通过；frontend、根配置及依赖锁文件未修改；未调用真实支付或付费模型。

## 完整修改文件清单

```text
backend/ALLOWANCES_V1.md
backend/README.md
backend/alembic/versions/0006_allowance_packages_and_task_accounting.py
backend/allowances.env.example
backend/app/api/v1/router.py
backend/app/core/config.py
backend/app/infrastructure/database/models.py
backend/app/infrastructure/process_runner.py
backend/app/infrastructure/queue/celery_app.py
backend/app/modules/ai/providers/bounded.py
backend/app/modules/ai/providers/openai_compatible.py
backend/app/modules/ai/worker.py
backend/app/modules/auth/dependencies.py
backend/app/modules/auth/repository.py
backend/app/modules/auth/router.py
backend/app/modules/auth/service.py
backend/app/modules/billing/__init__.py
backend/app/modules/billing/manage.py
backend/app/modules/billing/models.py
backend/app/modules/billing/policy.py
backend/app/modules/billing/repository.py
backend/app/modules/billing/router.py
backend/app/modules/billing/service.py
backend/app/modules/billing/transfer.py
backend/app/modules/billing/worker.py
backend/app/modules/download/child.py
backend/app/modules/download/router.py
backend/app/modules/download/worker.py
backend/app/modules/media/probe.py
backend/app/modules/tasks/models.py
backend/app/modules/tasks/repository.py
backend/app/modules/tasks/service.py
backend/app/modules/transcript/worker.py
backend/tests/conftest.py
backend/tests/test_allowances.py
backend/tests/test_allowances_postgres.py
```
