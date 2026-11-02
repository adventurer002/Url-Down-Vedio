# 管理后台阶段 A：后端权限、审计基础与只读查询

本文记录阶段 A 当时的范围。后续 B–D 已完成，当前接口、迁移和验收以 [ADMIN_BACKEND_MVP.md](ADMIN_BACKEND_MVP.md) 为准。

本阶段沿用 FastAPI、现有微信登录、PostgreSQL 和 uv，没有新增服务或依赖。仅修改 backend/。
前端 /admin 仍是原健康页面；本阶段接口通过 pytest、Swagger 和 curl 验收。

## 范围与模块边界

- admin 模块只编排 UserRepository、AllowanceRepository、AuditRepository 的明确契约。
- auth 模块负责用户查询、全局初始化互斥及角色变更；不向 HTTP 开放提升角色操作。
- billing 模块提供管理员纯读取契约，不复用有初始化副作用的 balance/lock_account/daily。
- audit 模块持有 audit_events。首次管理员提升与审计在同一事务提交；审计失败整体回滚。
- 没有新增账号停用、恢复、额度发放、全局任务管理或审计查询 HTTP 接口，留给后续确认阶段。
- 用户接口原有任务/文稿/问答/文件归属校验保持生效；管理员角色不自动获得私有内容访问权。

## 数据迁移与本地启动

0008 仅增加 audit_events 表及索引，不重写用户、额度、任务。迁移不会自动创建管理员。
已在独立 PostgreSQL schema 验证 0007 → head → check → 0007 → head → check，并检查原用户保留。
正式业务库不要执行 downgrade：它会删除审计表，而且不会撤销已经提升的管理员角色。

以下命令只用于本机开发数据库；先确认本地配置指向开发环境。不要用 5175 的生产代理做写入验收。
生产按既有 CI/CD 备份、迁移和发布流程操作，不能用模板覆盖 /etc/videohub/backend.env。

在 backend/：

```sh
uv run alembic upgrade head
uv run alembic check
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

只读管理查询和初始化 CLI 不需要启动 Worker；如果还要验证业务任务，另开终端运行唯一一个 Worker：

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

## 初始化首个管理员

先通过已有 auth 登录流程绑定本机开发账号，从 GET /api/v1/auth/me 取得用户 UUID。
开发 mock 只限开发环境，生产不允许为了后台登录而开启 mock。正式回调未就绪时，不能用此命令绕过登录创建身份。

在 backend/ 执行（占位符换成已有账号 UUID）：

```sh
uv run python -m app.modules.admin.manage bootstrap \
  --user-id '已有账号UUID' \
  --actor 'local-operator' \
  --reason '初始化项目管理员' \
  --key 'bootstrap-admin-001'
```

条件：目标存在、status=active、role=user、已有 last_login_at，且数据库尚无任何管理员。
即使已有管理员已停用，也拒绝再次初始化，避免此命令成为接管账号的通道。恢复机制另行设计。
操作人和原因只填运维标识与业务说明，不填密码、Token、微信身份或密钥。

PostgreSQL 使用事务级 advisory lock 串行化首次初始化；不能只锁目标用户，否则两个不同账号会同时成功。
同一 key、目标、操作人、原因重复执行返回相同 audit_id 和 duplicate=true；同键不同内容返回
IDEMPOTENCY_CONFLICT，新键再提升返回 ADMIN_ALREADY_INITIALIZED。重放成功记录不重新提升或恢复账号。
数据库/网络超时可能让调用方暂时不知道提交结果，应保留原 key 重试，不能换 key 猜测。
SQLite 仅用于离线测试，以 BEGIN IMMEDIATE 保护同样的初始化边界。

CLI 是需要应用数据库访问权的运维命令，不是网页功能。生产仅由具备应用运维权限者执行；
现有 videohub-deploy 账号的 sudo 权限仍只允许固定部署辅助程序，本阶段没有增加远程命令权限。
命令失败返回非零状态，输出脱敏错误；不会输出连接串、Token 或底层异常详情。

## 接口

以下接口全部要求活跃管理员，每次通过现有鉴权读取数据库身份。
即使 AUTH_REQUIRED=false，管理接口也不会匿名放行。
未登录 401、普通用户 403、停用管理员 401；Cookie 和 Header Bearer 沿用现有只读鉴权。
管理接口不接受 Query Token。成功和错误仍使用 data/error/request_id 规范。

| 方法与路径（/api/v1/admin） | 参数与作用 |
| --- | --- |
| GET /users | limit 1–100、offset >= 0；q 最多 100 字，按昵称大小写不敏感的文字子串或完整 UUID；status=active/disabled、subscribed=true/false、member=true/false，可组合 |
| GET /users/{id} | 用户白名单字段和会员有效期；未知用户 404 |
| GET /users/{id}/allowances | 额度摘要、当天使用量及 lots 分页，limit 默认 20；允许查询停用或未绑定账号 |
| GET /users/{id}/ledger | 额度业务流水分页，limit 默认 50；未知用户 404 |

用户列表按 created_at DESC、id DESC 稳定排序，返回 items/total/limit/offset。
q 中 % 和 _ 按文字匹配，不作为 SQL 通配符。会员状态表示 member_until 尚未到期，
与账号 active/disabled 分开展示；停用不改变会员合同有效期。没有账户的用户归为非会员。
用户返回 id、nickname、is_subscribed、role、status、created_at、last_login_at、member、member_until；
不返回 OpenID、UnionID、身份指纹、登录凭证或私有正文。

额度响应说明：

- account_exists=false 表示尚无额度账户；返回零余额，不创建账户或补发体验。
- balances 的 asr_seconds 单位为整数秒，ai_generations 单位为次数。
- available 是当前生效、未过期且扣除 used/held 后的余额，取关时排除 trial；未来账期不计入。
- usable 还检查账号活跃状态，停用时为 0；available 仍可供管理员查看保留权益。
- held 包括执行中占用，即使原额度批次已过期也保留显示，不擅自释放。
- lots.items 显示批次 id、source、granted、used、held、usable、starts_at、expires_at、expired、future；
  total 是全部批次数，按到期时间、id 排序。未发生过到期登记的批次也能按当前时间显示 expired。
- daily.date 使用北京时间；没有当天记录时返回 0，不写 daily_allowances。
- member/limits 是现有产品规则，不等于停用账号获准继续使用。
- 所有查询只执行 SELECT，不领取体验、不记录过期事件、不恢复排队任务、不调用 Redis 或模型。

## curl / Swagger 验收

访问 http://127.0.0.1:8000/docs，在 auth 分组按 STAGE9.md 完成本地登录，浏览器 Cookie 可用于 GET 管理接口。
初始化前应返回 403，CLI 初始化后再次请求应返回 200。用另一普通账号验证 403。
无新增 HTTP 提权、发放或取消入口。

curl 可沿用已有登录生成的 Cookie 文件；下面不打印登录凭证：

```sh
BASE='http://127.0.0.1:8000/api/v1'
COOKIE_FILE='/tmp/videohub-cookies.txt'
USER_ID='从本机auth/me取得的账号UUID'

curl --max-time 5 -i "$BASE/admin/users"
curl --max-time 5 -b "$COOKIE_FILE" "$BASE/admin/users?limit=20&member=false"
curl --max-time 5 -b "$COOKIE_FILE" "$BASE/admin/users/$USER_ID"
curl --max-time 5 -b "$COOKIE_FILE" "$BASE/admin/users/$USER_ID/allowances?limit=20"
curl --max-time 5 -b "$COOKIE_FILE" "$BASE/admin/users/$USER_ID/ledger?limit=50"
```

第一条预期 401；其余在管理员登录下 200。不要把 Cookie 文件加入 Git 或分享其内容。
本机 5175 是部署静态快照且连接生产，代码尚未发布前其中不会出现本阶段新接口。

## 审计基础的保证与边界

成功初始化记录包含 operator 操作人标识、目标 UUID、原因、cli 请求编号、幂等键、时间、结果和角色前后值。
白名单 changes 仅为 role 的 user → admin，不复制原始请求或完整用户模型。
审计仓储加入调用者事务，不独立提交；没有修改、删除或 HTTP 查询接口。
本阶段只落地成功初始化的持久审计，拒绝/失败由 CLI 返回错误；统一安全事件和后续后台操作审计留到相应阶段。
现有 billing grant CLI 尚未在本阶段改造，不能把它的现有流水当作已接入统一审计。
这是应用层追溯能力，不声称能抵御有数据库写权限的管理员直接篡改数据。

## 验证与文件清单

在 backend/：

```sh
uv run pytest
uv run pytest tests/test_admin.py tests/test_admin_postgres.py -q
```

测试使用临时 SQLite、独立 PostgreSQL schema，以及已有离线夹具，不连接生产服务器，不调用付费模型。
专项覆盖真实鉴权、角色实时读取、Query Token 拒绝、用户私有任务隔离、筛选/分页/输入校验、
纯 SELECT 查询、停用/取关/未来/过期/占用额度、首次初始化竞争、幂等冲突、审计失败回滚和真实迁移一致性。

本阶段验证（2026-10-26）：完整后端 312 passed，无跳过；管理专项 11 passed。
真实 PostgreSQL 并发初始化与迁移升降级、alembic check 均通过，业务库和生产库未迁移。
Alembic 测试有 6 条现有配置缺少 path_separator 的弃用提示，不影响迁移或测试通过。
uv 缓存因本机沙箱权限临时使用 /tmp/videohub-uv-cache，依赖及锁文件没有变化。

新增：

- app/modules/admin/{__init__,service,router,manage}.py
- app/modules/audit/{__init__,models,repository}.py
- alembic/versions/0008_admin_audit_foundation.py
- tests/test_admin.py、tests/test_admin_postgres.py、ADMIN_STAGE_A.md

修改：

- app/modules/auth/repository.py
- app/modules/billing/repository.py
- app/api/v1/router.py
- app/infrastructure/database/models.py
- README.md

前端、依赖锁、生产配置、部署辅助程序和 systemd 模板均未修改。
