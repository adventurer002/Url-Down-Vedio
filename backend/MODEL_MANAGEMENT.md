# 模型管理 V1

管理后台 `/admin/models` 提供服务连接、模型版本、异步能力测试、功能分配、原子发布及历史回滚。
当前支持 OpenAI Chat Completions 兼容文本接口、OpenAI Audio Transcriptions 兼容转写、千问专用 ASR；不支持任意 SDK、脚本、自定义请求体或内网服务地址。

## 使用顺序

1. 添加服务连接：名称、后端适配类型、公开 HTTPS Base URL、API Key。
2. 添加模型：选择连接的具体版本，填写服务商模型 ID，设置超时和文本 Token / 音频上传上限。文本输入、输出单价可选，单位为美元 / 百万 Token，仅用于总结与导图成本估算。
3. 保存后创建不可变的新版本，尚不影响项目调用。编辑连接也不会自动替换已有模型所引用的连接版本。
4. 分别发起连接测试、业务兼容测试。测试通过统一 TaskService 入队，由 Celery Worker 执行；使用固定合成语音和文稿，不读用户媒体、不扣用户额度，真实服务测试仍可能收取服务商费用。
5. 在“功能分配”选择通过相应能力测试的版本，填写原因并确认后统一发布。尚未启用的剪辑功能隐藏，启用剪辑后需分配剪辑分析及精确定位能力。
6. 历史分配可重新发布为一个新版本。回滚同样验证归档、密钥和最新兼容测试状态，不能恢复已撤销的密钥。

文本兼容测试实际调用生产总结、思维导图、问答证据及剪辑选择校验器，按功能记录通过情况；部分通过时仅能分配已通过的功能。
ASR 测试使用仓库内固定英文合成语音（“VideoHub model test. The meeting starts at nine.”），检查识别文本；精确定位还需检查有效时间轴。千问的粗时间段不能作为精确定位能力。
测试结果只证明固定样例与现有适配器的兼容性，测试时间、耗时和失败原因分别展示；生产调用的统计独立计算。

## 首次配置与启动

本次开发仅在独立 PostgreSQL QA schema 中迁移和操作，没有更新现有开发/线上数据库，也没有推送、合并或部署。
默认 `MODEL_CONFIG_ENABLED=false` 保留原环境配置行为。启用后尚未发布时同样沿用原环境配置；首个版本发布后，未分配的 ASR 能力明确停用，不隐式回退到环境密钥。

先备份数据库，配置 PostgreSQL/Redis 后：

```sh
cd backend
uv sync
uv run alembic upgrade head
# 在本地生成独立 Fernet 主密钥，将输出填入私有 .env；不要提交密钥。
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

在 `.env` 配置：

```dotenv
MODEL_CONFIG_ENABLED=true
MODEL_CONFIG_ENCRYPTION_KEY=<独立Fernet主密钥>
MODEL_CONFIG_PREVIOUS_KEYS=
```

API 和 Worker 必须使用相同的加密密钥和数据库。首次变更上述环境设置后重启两个进程；之后网页模型发布无需重启：

```sh
# backend 目录，一个终端
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
# backend 目录，另一个终端，M1 8GB 开发约定
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
# frontend 目录
pnpm install --frozen-lockfile
pnpm dev
```

既有 `LLM_*`、`ASR_*` 和 `CLIPS_TIMING_*` 环境变量继续作为未发布时期的兼容配置，网页不会读取并回显其密钥，也不直接改写 .env。

## 一致性与缓存

- 服务连接与模型各有不可变版本；发布保存完整功能映射，只在一个事务中切换激活指针。
- 确认创建任务时校验报价的发布版本；配置变化需要重新确认报价。已经创建的任务重试仍返回原任务。
- 任务仅保存发布 ID 和模型版本 ID，不保存密钥、密文或地址。API 与 Worker 使用同一解析契约，按任务快照构建适配器。
- 缓存模型身份使用版本指纹，包含服务连接版本的引用关系。同名模型换服务商或参数，不会混用原结果。
- 总结、导图可分别分配模型；导图任务的总结阶段走总结模型，树生成走导图模型，两者共享任务调用预算，组合缓存包含两个版本身份。
- 归档不改写任务历史和已创建任务。当前发布仍引用的模型或连接不能归档。
- 发布使用配置代数 CAS，资源编辑使用资源版本 CAS；同一操作键可安全重放结果，不能换内容复用。

## 密钥与网络

密钥使用独立 Fernet 主密钥加密，API 不返回原文或密文，审计只记录变更标志及不可逆请求指纹。密钥不写入任务、浏览器持久化存储、调用统计或测试结果。

正常轮换：编辑连接输入新密钥 → 编辑模型选择新连接版本 → 测试 → 发布。旧任务继续使用原凭据版本。
紧急撤销：在当前或历史密钥版本中选择撤销，后续请求会被拒绝；已在网络中发出的请求可能完成。历史回滚不会取消撤销标记。旧密钥需要在服务商侧同步撤销。
主密钥轮换：新主密钥放在 `MODEL_CONFIG_ENCRYPTION_KEY`，旧主密钥临时放入 `MODEL_CONFIG_PREVIOUS_KEYS` 以解密历史版本；应保留可用旧解密密钥，直至不再需要旧凭据历史。

仅允许公开 HTTPS 地址，拒绝 URL 用户名密码、查询参数、片段、本机及私网 IP。请求沿用公共网络传输层的 DNS 二次校验、不跟随重定向、连接/总超时和有界重试。
测试任务支持协作取消；任务执行前及执行中重新检查发起管理员的角色、账号状态与凭证版本。

## 测试和运行统计

模型测试调用与运行调用分开记录。近 24 小时显示真实模型操作次数、成功率和平均耗时，不把无数据展示为 100% 成功。
统计只保存版本、用途、状态、耗时和错误码，不保存文稿、音频或请求/响应原文。ASR 的一次转写操作可包含适配器内部多个音频分段。
统计写入为限时的尽力记录，失败不会改变已成功的业务结果；历史统计在运行调用中采样分批清理 30 天前记录。

测试和业务任务沿用队列恢复流程。遇到 broker / Worker 中断，检查 Worker 与审计；可按项目既有维护方式执行：

```sh
cd backend
uv run python -m app.modules.billing.manage reconcile
```

## Swagger / curl 验证

Swagger 为 `http://127.0.0.1:8000/docs`。管理写操作必须使用正常管理员的 Header Bearer，cookie-only 会话只能查看。
以下 Token、模型 ID 和密钥均是占位值；只有接入真实服务商并通过真实测试后才发布。

```sh
curl http://127.0.0.1:8000/api/v1/admin/models \
  -H 'Authorization: Bearer <ADMIN_TOKEN>'
curl -X POST http://127.0.0.1:8000/api/v1/admin/models/connections \
  -H 'Authorization: Bearer <ADMIN_TOKEN>' -H 'Content-Type: application/json' \
  -d '{"key":"connection-1","reason":"配置模型服务","name":"主力服务","adapter":"openai_chat","base_url":"https://<PUBLIC_API_HOST>/v1","api_key":"<VENDOR_API_KEY>"}'
curl -X POST http://127.0.0.1:8000/api/v1/admin/models/models \
  -H 'Authorization: Bearer <ADMIN_TOKEN>' -H 'Content-Type: application/json' \
  -d '{"key":"model-1","reason":"新增总结模型","name":"总结模型","connection_revision":"<CONNECTION_REVISION_UUID>","model_id":"<MODEL_ID>","timeout":90,"max_tokens":4096}'
curl -X POST http://127.0.0.1:8000/api/v1/admin/models/tests \
  -H 'Authorization: Bearer <ADMIN_TOKEN>' -H 'Content-Type: application/json' \
  -d '{"key":"test-1","reason":"验证业务兼容","revision_id":"<MODEL_REVISION_UUID>","phase":"compatibility"}'
```

读取 `/admin/models` 查看测试终态和能力。连接测试使用 `phase=connection`。取消测试沿用 `POST /api/v1/admin/tasks/{task_id}/cancel`，需 `key` 和 `reason`。

发布接口 `POST /api/v1/admin/models/publish`：

```json
{
  "key": "publish-1",
  "reason": "切换已验证的模型",
  "expected_version": 0,
  "bindings": {
    "summary": "<MODEL_REVISION_UUID>",
    "mindmap": "<MODEL_REVISION_UUID>",
    "question": "<MODEL_REVISION_UUID>",
    "clip_analysis": null,
    "transcription": null,
    "clip_timing": null
  }
}
```

`expected_version` 来自最新目录；剪辑启用时需填写相应功能绑定。回滚传历史 `release_id` 和当前 `expected_version`，服务端重新验证后创建新发布。
编辑连接/模型传 `id`、`expected_version`，产生新 revision；连接不填 `api_key` 表示沿用原密钥。
归档为 `POST /admin/models/resources/{id}/archive`；紧急撤销为 `POST /admin/models/credentials/{id}/revoke`，均需操作键和原因；归档还需资源当前版本。

## 数据迁移与文件清单

迁移 0013 新增模型资源、版本、加密凭据、发布、激活状态、测试任务引用、模型操作统计七张表。存在模型资源时拒绝直接降级，避免丢失密钥版本、发布和任务追溯。

- 新增：`app/modules/model_config/{models,security,runtime,service,router,worker}.py`、固定测试语音 `assets/probe.wav`、迁移 0013、模型配置单元及 PostgreSQL 并发测试。
- 调整：模型工厂、AI/ASR/问答/剪辑的 API 和 Worker、报价/任务创建快照及缓存身份、任务恢复、审计契约和数据库注册；新增 cryptography 依赖与双环境模板。
- 前端新增：`admin/ModelsPage.tsx`、`modelApi.ts`、`models.css` 和管理页面测试；调整管理导航、审计标签和页面名称。
- 本地 `.runtime` QA 包装及示例配置只属于独立测试环境，不随代码交付，也没有激活示例模型。

## 模型管理优化（六项）

1. 目录按“模型版本 + 测试类型”读取最新测试，同时保留执行中的测试；其他模型产生大量记录不会使已验证模型变成未测试。模型草稿所引用的旧连接版本同样保留，避免编辑连接超过 50 次后错误判断旧模型的密钥不可用。
2. 同一模型版本的同类测试只允许一个排队/执行任务；全局最多三项排队或执行，每个管理员仍限每小时二十次。后端锁定配置状态后检查，防止并发点击绕过限制。尚未创建任务的预约有效期为两分钟；过期后显示失败并要求重新发起，不持续轮询，也不能重放过期预约绕过排队限制。
3. 新增管理员只读接口 `GET /api/v1/admin/models/credentials/{id}/impact`，返回共用密钥的全部连接/模型版本、当前引用功能及配置代数，不包含密钥或密文。撤销弹窗必须成功加载影响范围后才能确认；刷新影响范围会清除核对勾选。网页撤销请求携带 `expected_version`，功能分配变化时拒绝使用过期预览继续撤销。
4. 发布前检查模型、连接、密钥、类型及兼容能力，展示具体阻断原因和逐项变更；无变更时不提交。首次发布停用未分配的转写能力会明确提示。成功后先重新读取配置，再清空草稿，避免把刚发布的结果误判为版本冲突。
5. 服务连接保留“编辑”，归档和撤销移入“更多”；模型保留编辑与测试，归档移入“更多”。危险操作在菜单中固定为红色。窄屏使用带字段名称的卡片排列，操作区保留 12px 横向间隔。
6. 编辑抽屉关闭、功能分配重新载入、页面刷新及链接离开时提供未保存保护；继续编辑保留字段，确认放弃才清除。发布历史从既有审计补充操作人、原因和发布版本，无需增加数据库迁移。

新增前端文件：`admin/modelReadiness.ts`、`admin/useDiscardGuard.tsx`；本轮修改模型服务/API、TaskRepository 的活动测试查询契约、AuditRepository 的发布元数据查询、UserRepository 的显示名称查询，以及模型管理页面、样式、前后端专项测试。本节不涉及其他既有支付、会员或落地页改动。

启动方式沿用上文；现有 0013 数据表无需额外迁移。Swagger 可检查影响范围接口：

```sh
curl 'http://127.0.0.1:8000/api/v1/admin/models/credentials/<CREDENTIAL_UUID>/impact' \
  -H 'Authorization: Bearer <ADMIN_TOKEN>'
```

验证时先在连接的“更多”菜单打开撤销预览并取消，再到功能分配核对发布检查；不要为了验收而撤销正在使用的真实密钥。

### 六项优化验收结果（2026-11-08）

- 最终后端全量：`uv run pytest`，890 passed，353.69 秒。新增用例覆盖超出全局 100 条记录后的能力保留、并发重复预约、全局排队上限、过期预约、共享密钥影响范围与撤销代数检查、发布操作人/原因以及旧连接引用保留。
- 最终前端全量：31 个测试文件、373 项通过；类型检查、生产构建和公开/私有页面构建验证通过。页面专项共 12 项，包括发布成功刷新后的草稿一致性。
- `git diff --check` 通过。桌面实际读取危险菜单项颜色为 `rgb(185, 28, 28)`；密钥影响范围预览可正常加载，未执行实际撤销。
- 实际验收编辑抽屉的继续编辑、放弃修改及嵌套弹窗层级；390 × 844 下页面宽度为 390、卡片宽度为 332，无横向溢出，已恢复默认视口。桌面长模型 ID 和 API 地址可换行，不与相邻列重叠。
- QA 仍只使用独立 schema 中明确标注的未接入、未发布示例；未接入真实服务商、未发布配置，未推送、合并或部署。QA API 和专用 Worker 已加载最终后端代码。

## 回归

后端目录 `uv run pytest`；前端目录 `pnpm type-check`、`pnpm test:unit`、`pnpm build`。
专项测试覆盖密钥加密和脱敏、请求重放、SSRF 地址限制、兼容测试门禁、完整发布/回滚、撤销后拒绝请求、分阶段模型路由、缓存身份、任务快照/报价重试、测试取消/权限失效、ASR 时间戳、事务回滚以及真实 PostgreSQL 并发。

### 本次验收结果（2026-11-08）

- 后端最终全量 `uv run pytest`：884 passed，333.41 秒；其中模型管理专项 22 项通过，覆盖 ASR 测试临时目录隔离和清理。
- 前端全量：31 个测试文件、366 项通过；最后文案调整后管理页面专项 16 项再次通过。`pnpm type-check`、`pnpm build` 及公开页面/私有页面 noindex 构建检查通过。
- `git diff --check` 通过。
- 独立 QA schema 实测了服务连接、模型录入、API → TaskService → 专用 Celery 队列 → 测试失败终态展示；未通过兼容测试的模型不可分配。验收地址为 `http://localhost:5183/admin/models`。
- 使用明确标注“未接入/未发布”的假地址和假密钥，没有接入或发布真实服务商配置；不能据此宣称真实模型请求成功。接入时仍需填写真实服务商配置并完成测试。
- 桌面列表及功能分配页面已目视验收；390 × 844 手机视口下页面与编辑抽屉无横向溢出，验收后已恢复默认视口。
- QA API 与专用 Worker 已重启加载最终代码。既有开发/线上数据库未迁移，未推送、合并或部署。

## 独立生产发布（2026-11-10）

本功能从独立 QA 工作区提取到最新 main；支付、订单、会员调整及用户删除不在本次发布内。模型表独立于这些模块，迁移编号改为 `0013`，直接继承已上线的 `0012`。原 QA 工作区的 `0013`–`0015` 是未发布草稿，后续整合支付/会员时必须重新编号并基于已合并迁移链调整，不能直接复制旧链。

部署沿用 PR → main CI → Deploy production。主密钥仅写入服务器私有配置，不能放入 Git、Actions 输出或前端。启用后尚未发布功能分配时，现有环境模型继续服务；模型供应商连接与功能分配由管理员验证后发布。
