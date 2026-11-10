# 模型管理独立发布检查 · 2026-11-10

本次从独立 QA 工作区提取模型管理功能到最新 main，保留原工作区。发布范围包括管理页面、连接与模型版本、异步连接/兼容测试、功能分配、历史回滚、密钥加密与撤销、运行统计，以及业务任务模型快照和缓存隔离。

不包含未提交的支付、订单、会员调整、用户删除或其他页面改动。迁移 0013 直接继承现有线上 0012，只创建七张模型管理表；原 QA 工作区的支付/会员迁移链未合并，后续需重新编号。

## 验证结果

- 后端 `cd backend && uv run pytest -q -ra`：861 passed，327.57 秒，无跳过。
- 前端 `cd frontend && pnpm exec vitest run --maxWorkers=2`：30 个文件、363 项测试通过。
- `pnpm type-check`、与 CI 配置一致的生产构建及公开资源验证通过。
- PostgreSQL 独立 schema：先迁移到 0012 并写入测试用户，再升级 head；用户保留，七张模型表和初始状态正确；`alembic check` 无模型差异，没有支付/订阅表；已清理 QA schema。
- `git diff --check`、新增文件秘密值模式扫描通过。固定语音样本随发布包交付。

## 部署与生产启用

按推送分支 → PR CI → 合并 main → main CI 发布包 → Deploy production 的既有流程发布。没有修改固定根部署程序或服务器 sudo 权限。

部署自动备份数据库后升级迁移。默认 `MODEL_CONFIG_ENABLED=false`；管理员需在服务器私有配置中启用并生成独立 Fernet 主密钥，API/Worker 使用相同配置后重启。加密主密钥不进入仓库或日志。启用但尚未发布功能分配时继续使用现有环境模型。

本次测试没有调用真实付费模型；生产连接由管理员填写真实供应商信息，经连接与对应业务兼容测试后发布。没有把 QA 假模型、假密钥或测试配置写入线上。

## 启动与验证

- API：`cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`。
- Worker：`cd backend && uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info`。
- 前端：`cd frontend && pnpm dev --port 5174`；管理员打开 `/admin/models`。
- 健康：`curl --fail --max-time 5 https://vedio.vip/api/v1/health/ready`。
- 管理目录：`curl --fail --max-time 10 https://vedio.vip/api/v1/admin/models -H 'Authorization: Bearer <ADMIN_TOKEN>'`；未登录必须拒绝访问。
- 完整 Swagger/curl 操作示例见 `backend/MODEL_MANAGEMENT.md`。

## 修改文件

- `backend/MODEL_MANAGEMENT.md`
- `backend/alembic/versions/0013_model_configuration.py`
- `backend/app/modules/model_config/__init__.py`
- `backend/app/modules/model_config/assets/probe.wav`
- `backend/app/modules/model_config/models.py`
- `backend/app/modules/model_config/router.py`
- `backend/app/modules/model_config/runtime.py`
- `backend/app/modules/model_config/security.py`
- `backend/app/modules/model_config/service.py`
- `backend/app/modules/model_config/worker.py`
- `backend/tests/test_model_config.py`
- `backend/tests/test_model_config_postgres.py`
- `frontend/src/admin/ModelsPage.tsx`
- `frontend/src/admin/modelApi.ts`
- `frontend/src/admin/modelReadiness.ts`
- `frontend/src/admin/models.css`
- `frontend/src/admin/useDiscardGuard.tsx`
- `frontend/src/tests/adminModels.test.tsx`
- `.env.docker.example`
- `.env.example`
- `backend/app/api/v1/router.py`
- `backend/app/core/config.py`
- `backend/app/infrastructure/database/models.py`
- `backend/app/infrastructure/queue/celery_app.py`
- `backend/app/modules/admin/router.py`
- `backend/app/modules/admin/service.py`
- `backend/app/modules/ai/providers/__init__.py`
- `backend/app/modules/ai/providers/bounded.py`
- `backend/app/modules/ai/repository.py`
- `backend/app/modules/ai/worker.py`
- `backend/app/modules/audit/repository.py`
- `backend/app/modules/auth/repository.py`
- `backend/app/modules/billing/manage.py`
- `backend/app/modules/billing/repository.py`
- `backend/app/modules/billing/service.py`
- `backend/app/modules/clips/service.py`
- `backend/app/modules/clips/worker.py`
- `backend/app/modules/knowledge/service.py`
- `backend/app/modules/knowledge/worker.py`
- `backend/app/modules/tasks/repository.py`
- `backend/app/modules/tasks/service.py`
- `backend/app/modules/transcript/providers.py`
- `backend/app/modules/transcript/service.py`
- `backend/app/modules/transcript/worker.py`
- `backend/pyproject.toml`
- `backend/uv.lock`
- `frontend/src/admin/AuditPage.tsx`
- `frontend/src/admin/admin.css`
- `frontend/src/admin/api.ts`
- `frontend/src/admin/ui.tsx`
- `frontend/src/utils/navigation.ts`
- `frontend/src/views/AdminView.tsx`
- `backend/reviews/model-management-release-2026-11-10.md`
