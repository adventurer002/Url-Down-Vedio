# 管理后台后端 MVP：用户、会员额度、任务与审计

完成日期：2026-10-26。本版覆盖确认范围内的后端阶段 A–D，采用模块化单体，没有新增服务、依赖或独立认证。
前端仍是原页面，本版尚未提交、推送或部署到服务器。备案、公众号公网回调和真实模型验收仍按原上线清单处理。

## 1. 功能与接口

以下路径均位于 `/api/v1/admin`。成功包裹 `data`，错误包含 `code/message/request_id`。
所有接口要求活跃管理员；普通用户 403，未登录或凭证失效 401。

| 方法与路径 | 功能 |
| --- | --- |
| GET /users | 用户分页，按昵称/完整 UUID、状态、关注、会员筛选 |
| GET /users/{id} | 用户详情、会员有效期和 status_version |
| GET /users/{id}/allowances | 纯读取余额、占用、批次与北京时间当日用量 |
| GET /users/{id}/ledger | 额度流水分页 |
| POST /users/{id}/status | 停用/恢复普通账号，有版本检查、原因、幂等键与审计 |
| GET /users/{id}/grants/preview?product=monthly | 只读发放预览，不锁定时间或初始化额度 |
| POST /users/{id}/grants | 发放 monthly、asr_120、ai_100 固定产品 |
| GET /tasks | 全局任务分页，按 user_id、status、task_type、since、until 筛选 |
| GET /tasks/{id} | 任务元数据、公开源视频标题/身份、额度占用和结算 |
| POST /tasks/{id}/cancel | 协作取消，取消意图与审计原子提交 |
| GET /audit-events | 按 actor_id、action、target_type、target_id、outcome、since、until 查询审计 |
| GET /overview | 用户、会员、任务、额度使用和数据库媒体登记统计 |
| GET /storage | 有预算的存储扫描、可用空间、配额和 TTL，不返回磁盘路径 |
| POST /storage/cleanup | 有审计和幂等保护的过期文件清理 |

保留原 `/api/v1/system/cleanup`，也走同一审计流程。原设置按钮的无请求体调用使用固定原因
“设置页清理过期文件”和服务端 request_id；可传 reason/key 获得跨请求幂等。所有 HTTP 清理操作现在均要求 Header Bearer。
系统定时器继续沿用原有独立清理/恢复命令，没有改动 systemd 或部署权限。

分页：limit 1–100、offset >= 0，返回 items/total/limit/offset；默认 20，流水和审计默认 50。
列表按 created_at DESC、id DESC 稳定排序，额度批次按 expires_at、id 排序。
用户 q 最多 100 字，昵称大小写不敏感，`%`/`_` 为普通文字；status=active/disabled，subscribed/member 为布尔值。
时间筛选必须带 `Z` 或 `+08:00` 等时区，采用 `[since, until)`，统一转换 UTC 查询；since 必须小于 until。

## 2. 权限与账号状态

继续使用 user/admin 两级。没有网页提权、删除用户、修改微信身份或任意 SQL/Shell 接口。
初始管理员仍用阶段 A 的受控 CLI，仅能从已登录绑定的活跃普通账号初始化；数据库已有管理员时不允许再次初始化。
不能在管理 API 停用/恢复管理员，包括自己；不通过关闭生产认证或打开 mock 来获得后台权限。

普通账号状态变更请求示例：

```json
{
  "status": "disabled",
  "expected_status": "active",
  "expected_version": 0,
  "reason": "用户请求停用账号",
  "key": "user-disable-001"
}
```

expected_version 来自最新用户详情的 status_version。状态/版本不匹配返回 USER_STATUS_CONFLICT；
重复原 key 返回原结果，不会在恢复后重新执行一次旧的停用操作。相同状态的操作不增加版本，审计 outcome=unchanged。
实际变更会递增 users.auth_version：停用、恢复后都要重新登录，旧 JWT 不会复活。
此前没有 ver 字段的 JWT 只兼容版本仍为 0 的账号；新登录携带当前版本。
微信已经确认但尚未兑换的会话也绑定版本；SSE 每次监控循环校验原版本。
普通 `/auth/me` 和登录响应不暴露内部 auth_version，管理员详情通过 status_version 提供并发控制信息。

停用阻止后续鉴权和新的执行准入；已经运行的任务和已开始的 HTTP 文件传输不被强行打断。
任务取消要单独操作，额度不能提前释放。准入和状态变更遵守 account → user 的锁顺序，等待账户锁后重新读取身份。
Swagger 新增 BearerAuth/Authorize 入口，既有 Cookie 只读登录仍兼容。所有管理写接口只接受 Header Bearer，拒绝 Cookie-only 和 Query Token。

## 3. 权益发放与账务事实

产品规则没有改变：月会员 300 分钟/100 次 AI，转写包 120 分钟，AI 包 100 次。
提前续费的额度从下一账期生效，月份锚点仍遵守现有日历规则；加购包从发放起 30 天有效。
不接受 amount、余额、过期时间、actor 等任意客户端字段。操作者 UUID 取自当前鉴权身份。

```json
{"product":"ai_100","reason":"人工补发权益","key":"manual-ai-001"}
```

预览不创建额度账户、不领取体验，不锁定最终生效时刻；最终发放事务会再次检查用户状态并返回实际起止时间。
同一 key/目标/产品/原因/操作者只发一次；同键不同操作返回 IDEMPOTENCY_CONFLICT。
同一幂等键在账号、发放、取消和清理之间也不能混用；`attempt_` 和 `cleanup-result:` 为保留前缀。
权益、会员日期、原额度流水与成功审计在同一事务提交；审计失败不能只留下已发放的权益。

原人工发放 CLI 已接入相同审计，新增必填 --reason：

```sh
uv run python -m app.modules.billing.manage grant \
  --user-id '已有账号UUID' --product monthly --key 'manual-membership-001' \
  --actor 'trusted-operator' --reason '人工发放月会员权益'
```

历史发放记录不会伪造补审计；使用其原 key 新发放会返回 LEGACY_GRANT_EXISTS，请查询历史流水。
仓储原 grant 契约保留供内部业务/测试使用，所有公开运维入口均使用审计编排，不提供未审计 HTTP 发放口。

发放后最多用 6 秒尝试唤醒排队任务；失败返回 dispatch_pending=true、dispatched=null，权益已经提交，
由既有 reconcile 定时恢复或用户 resume 补偿，不能因为唤醒失败再发一份。重复原请求也可再次尝试唤醒，不重复发放。

所有结果明确 mode=operator_grant、payment_verified=false。没有支付接入、现金退款、营收或已核验供应商成本；
overview 的 revenue/supplier_cost 为 null，不把人工发放金额当作收入。

管理员查额度仍只执行 SELECT，不补体验、不触发到期流水。available 表示当前有效且扣除 used/held 的保留权益，
取关排除 trial、未来批次不计入；usable 还检查账号活跃，停用时为 0。held 保留过期批次中仍在执行的占用。

## 4. 任务管理与取消

管理列表包括用户已隐藏的历史和旧匿名任务。只返回白名单元数据，不返回原始 input/result payload、错误全文、
进程 PID/主机、源 URL、签名参数、问答正文、文稿或收藏。result_references 只保留验证过的 UUID。
视频信息也只含 id/platform/platform_video_id/title/duration，不返回 URL、封面 URL 或原始解析信息。
管理员角色不改变用户端的资源归属校验，知道结果 UUID 也不能直接读取其他用户私有结果。

进度读取 Redis 匹配 task_id/status 的快照；全部快照合计预算 1.5 秒，Redis 故障仍返回 PostgreSQL 状态，seq=0。
queue_seconds 和 execution_seconds 分开显示，wait_reason 区分等待额度、容量或权益。

取消仍经 TaskService 和 TaskRepository：

- queued 可直接 cancelled；running/processing 仅登记 cancel_requested，Watchdog 确认退出后才提交最终状态和释放额度。
- 完成与取消使用原 CAS；晚到取消返回 completed，审计 unchanged，不删除已完成结果或反向核销。
- Redis 通知失败时返回 notification_pending=true，但 PostgreSQL 的取消意图和审计仍有效，Worker 可回查。
- 不调用 revoke(terminate=True)，不提供强杀、任意重试、批量取消或代用户创建收费任务。

取消审计保存请求发生时的事实，accepted 不代表进程已退出；审计查询另附 current_task_status 供关联最终状态，不改写原事件。

## 5. 审计与不确定结果

审计包含操作者、动作、对象、原因、request_id、幂等键、时间、白名单请求数据、必要前后值和结果。
动作包括 admin.bootstrap、user.status、allowance.grant、task.cancel、storage.cleanup.request/finish。
outcome 为 succeeded、accepted、unchanged、failed 或 unknown。
不存配置、密码、Token、OpenID/UnionID、原始任务内容或底层异常文本；操作人/原因也不要填写这些内容。

成功写入与业务变更原子提交。认证后发生的业务失败尽力记录单独的 attempt_ 事件，原操作 key 放在 request_data.operation_key，
不消耗原幂等键；数据库本身不可用时不能保证失败日志也成功，但不能因此提交无成功审计的业务变更。
匿名、无权限或参数验证阶段的拒绝仍由统一 HTTP 错误处理，不通过伪造操作者写审计。

提交确认丢失时先用新事务查询同 key 的成功审计；存在且完整匹配请求则按已提交结果重放。
数据库结果仍无法确认时标记 unknown/DATABASE_OUTCOME_UNKNOWN，不能把网络错误视作“没有发放”。
操作者应保留原 key 重试或核查审计，不换 key 重复操作。
没有审计修改/删除 HTTP 接口；这是应用级追溯，不声称能抵御有数据库写权限的管理员篡改。

## 6. 清理、存储与概览

手动清理复用原资源锁和安全清理：有效文件、运行进程组和活任务受保护。先提交清理意图，再执行文件操作，最后追加结果审计。
相同 key 不重复执行物理清理。失败可能已处理部分文件，因此使用 failed_or_partial；进程崩溃留下 accepted 或
结果审计无法写入时返回 outcome_unrecorded/requires_review，不伪造成功，也不盲目重跑同 key。
成功清理仍兼容 files/temporary_directories/orphan_files/freed_bytes/used_bytes 字段。
清理在 HTTP 工作线程执行，协程预算 90 秒，不做自动重试；超时不保证文件操作全部回滚。大量积压建议使用既有定时器。

存储查询扫描临时目录和成品，最多 1 秒或 100000 个文件，HTTP 总预算 2 秒；不创建目录、不返回路径、不跟随目录软链接。
不完整时 scan_complete=false、used_bytes=null，observed_bytes 仅为已观察部分，不能当成完整占用。
overview 的 registered_bytes 来自数据库，明确不包含临时/孤立文件，不代替磁盘扫描。
业务日以 Asia/Shanghai 零点重置；用户和会员总数、累计核销/占用、任务全状态统计均来自真实数据库。
Worker 只声明配置 solo/1，liveness=not_measured；不以 Solo 执行长任务时无法响应控制消息推断 Worker 已死。

## 7. 本地启动与 Swagger/curl

先确认配置是本机开发数据库，不要对 5175 的生产代理做写入验收。保留服务器私有配置，未部署前不运行生产迁移。
在 backend/：

```sh
uv run alembic upgrade head
uv run alembic check
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

如需执行视频任务，另一个终端运行唯一的 Worker：

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

通过既有微信登录流程绑定本机账号，然后仅首次执行：

```sh
uv run python -m app.modules.admin.manage bootstrap \
  --user-id '已有账号UUID' --actor 'local-operator' --reason '初始化管理员' --key 'bootstrap-admin-001'
```

Swagger：http://127.0.0.1:8000/docs。使用自己的本地登录凭证在 Authorize 中填写 Bearer Token，
依次查看用户、权益、发放预览、发放、状态和任务接口。Cookie-only 可以做只读查询，写操作需要 Header。
恢复账号时以停用响应的新 status_version 为 expected_version。

以下为 Bash 本地验收示例；凭证只读入终端变量，不写入仓库或分享输出：

```sh
BASE='http://127.0.0.1:8000/api/v1'
USER_ID='本机普通账号UUID'
TASK_ID='本机测试任务UUID'
read -r -s TOKEN

curl --max-time 5 -i "$BASE/admin/users"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/admin/users?limit=20"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/admin/users/$USER_ID/allowances"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/admin/users/$USER_ID/grants/preview?product=ai_100"
curl --max-time 12 -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"product":"ai_100","reason":"本机测试发放","key":"local-grant-001"}' \
  "$BASE/admin/users/$USER_ID/grants"
curl --max-time 8 -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"status":"disabled","expected_status":"active","expected_version":0,"reason":"本机停用测试","key":"local-disable-001"}' \
  "$BASE/admin/users/$USER_ID/status"
curl --max-time 8 -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"reason":"本机取消测试","key":"local-cancel-001"}' "$BASE/admin/tasks/$TASK_ID/cancel"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/admin/tasks?status=queued"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/admin/audit-events?limit=50"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/admin/overview"
curl --max-time 5 -H "Authorization: Bearer $TOKEN" "$BASE/admin/storage"
unset TOKEN
```

未知用户/任务 404；普通账号访问管理接口 403；匿名 401；同键不同操作/过期状态 409。
重复发放同一个请求只增一次权益；停用再恢复后旧凭证仍为 401。
真实任务可能调用付费模型，功能学习优先使用自动化隔离测试，不通过生产创建任务来证明后台接口可用。

## 8. 迁移与发布边界

- 0008 新增审计表；0009 新增 users.auth_version 和审计 request_data/result，默认值保持旧用户数据兼容。
- 没有自动创建管理员、重写用户身份或清空账务。初始化管理员需有应用运维权限，不扩大 videohub-deploy 的 sudo 范围。
- 新版本 API/Worker 必须先完成迁移并一起更新，不能长期混用不校验 ver 的旧 API。
- 生产已有代码版本的部署辅助程序在迁移变化后不会盲目自动回滚；仍按现有停写、备份、迁移和人工恢复边界发布。
- 不在业务库执行 downgrade。撤销 0009 会丢失凭证失效版本，回退代码也可能恢复旧 Token 的可用性，须人工评估登录会话处理。
- 本次只在独立测试 schema 验证迁移升降级和 alembic check，开发公共 schema 和生产库没有迁移。
- 不修改 root 部署辅助程序、systemd、.env、SSH 权限或 5175 预览静态快照。

## 9. 最终验证

按项完成后做专项/完整回归，再执行大检查：

| 验证 | 结果 |
| --- | --- |
| 后端完整 pytest | 338 passed，无跳过，无警告；包含 37 项新增管理测试 |
| PostgreSQL 并发 | 首个管理员、状态版本竞争、同键发放、取消/完成竞争、准入身份刷新通过 |
| PostgreSQL 迁移 | 隔离 schema 的 0007 → head → check → 0007 → head → check 通过，旧用户保留 |
| 实际 curl HTTP 烟测 | 临时 SQLite/存储的 Uvicorn 服务，23 次检查通过，覆盖 200/401/403、发放幂等、旧凭证失效、任务取消、清理及 OpenAPI |
| 前端兼容回归 | 83 passed，类型检查通过；业务源码未修改 |
| 前端生产构建 | 通过，API 基址 /api/v1，使用已有依赖构建，未改变 5175 预览快照 |
| 差异检查 | git diff --check；保留原有交接和预览改动 |

烟测服务已停止、临时业务数据已删除；没有调用付费模型/支付，也没有操作生产账号。
前端测试有 Node 对 localStorage 的实验特性提示，测试通过；Alembic 的 path_separator 兼容提示已通过配置消除。
uv 缓存临时使用 /tmp/videohub-uv-cache，没有重装依赖或改锁文件。

## 10. 修改文件清单

新增：

- app/modules/admin/{__init__,service,router,manage}.py
- app/modules/audit/{__init__,models,repository}.py
- app/modules/system/service.py
- alembic/versions/0008_admin_audit_foundation.py
- alembic/versions/0009_admin_mutations_and_credential_versions.py
- tests/test_admin.py、test_admin_status.py、test_admin_grants.py、test_admin_tasks.py、test_admin_operations.py、test_admin_postgres.py
- ADMIN_STAGE_A.md、ADMIN_BACKEND_MVP.md

修改：

- app/api/v1/router.py、app/infrastructure/database/models.py
- app/modules/auth/{models,repository,dependencies,tokens,service,router}.py
- app/modules/billing/{repository,manage}.py
- app/modules/tasks/{repository,service,router}.py
- app/modules/media/repository.py、app/modules/video/repository.py、app/modules/system/router.py
- tests/test_history_and_security.py、alembic.ini、README.md、ALLOWANCES_V1.md

已有 frontend/README.md、frontend/vite.server-preview.config.ts、deploy/HANDOFF_ADMIN.md 原样保留，不并入本版业务修改。
