# VideoHub AI — 阶段 1 后端底座

本阶段提供 FastAPI、PostgreSQL / Redis 就绪检查、Celery 测试任务与 Alembic 框架。
尚无视频解析、下载、转写或 AI 业务，没有前端。

## 1. 环境与依赖

使用 Python 3.14 和 uv。所有 Python 命令在 `backend/` 执行。
原始根目录 uv 初始化文件已移入 `backend/`；根目录已有 `.venv` 不再用于本工程，
如终端仍激活旧环境，先执行 `deactivate`，再使用下列命令（uv 自动管理 `backend/.venv`）。

在项目根目录：

```sh
cp .env.example .env
cd backend
uv sync --locked
```

配置始终读取项目根目录 `.env`，环境变量优先。
`STORAGE_LOCAL_PATH=./storage` 按项目根目录解析，不随启动目录变化。
默认配额 5120 MB，文件保留时间 24 小时；本阶段仅配置，下载前检查与过期清理待后续存储阶段实现。

## 2. 启动 PostgreSQL 与 Redis（任选一种）

### Homebrew 原生服务

```sh
brew install postgresql@16 redis
brew services start postgresql@16
brew services start redis
psql postgres -c "CREATE USER videohub_user WITH PASSWORD 'videohub_pass';"
createdb -O videohub_user videohub_dev
```

仅首次创建用户和数据库；若已存在，无需重复执行。默认密码仅用于本地开发。

### Compose 依赖服务

在根目录执行（不要同时运行占用同端口的 Homebrew 服务）：

```sh
docker compose up -d --wait
docker compose ps
```

Compose 仅运行 PostgreSQL 16 和 Redis 7，端口绑定宿主机环回地址，数据存入命名卷。
FastAPI / Worker 在宿主机运行时仍使用 `.env.example` 的 `localhost`。
`.env.docker.example` 用于将来在同一 Compose 网络内运行的应用进程，使用 `postgres` / `redis` 服务名；
当前不包含应用容器，不要将该模板直接用于宿主机进程。

## 3. 迁移与启动

终端一（在 `backend/`）：

```sh
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Alembic 已关联 `Base.metadata` 和配置 URL，本阶段没有业务表或迁移版本。

终端二（在 `backend/`）：

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

M1 8GB 使用 Solo 单并发。测试任务不包含外部耗时操作；后续任务必须实现协作取消与进程组 Watchdog，
不得依赖 `revoke(terminate=True)` 或仅靠 Celery 超时。

## 4. 验证

```sh
curl -i --max-time 5 http://127.0.0.1:8000/api/v1/health/live
curl -i --max-time 5 http://127.0.0.1:8000/api/v1/health/ready
curl -i --max-time 5 http://127.0.0.1:8000/api/v1/not-found
```

Swagger：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)，展开两个 health 接口点击 Try it out / Execute。

- live：仅检查进程，200，`{"data":{"status":"alive"}}`，无需 PG/Redis 存活。
- ready：并发检查 PG 和 Redis，每项总超时 1.5 秒，不自动重试；200 返回 `data.status/database/redis`。
- 任一依赖不可用：503，`error.code=DEPENDENCY_UNAVAILABLE`，message 仅含两项状态，不返回底层错误或连接串。
- 不存在的路径：404，`error.code=NOT_FOUND`。
- 所有请求生成 `req_...` ID，返回 `X-Request-ID`；错误体中的 `request_id` 与该响应头一致。

验证故障分支可在独立 API 进程使用 `REDIS_URL=redis://localhost:6399/0` 启动，
再访问 ready（预期 503）与 live（仍为 200）。请勿覆盖已有实例的环境。

验证 Celery 完整 broker → Solo Worker → result backend 链路（Worker 已运行，在 `backend/`）：

```sh
uv run python -c 'from app.infrastructure.queue.celery_app import celery_app; r = celery_app.send_task("system.ping"); print(r.get(timeout=10))'
```

预期输出 `{'pong': True}`。连接与发布失败使用有限重试，结果等待上限 10 秒；结果保存 1 小时。
此命令真实使用 Redis 与 Worker，不是直接调用任务函数。

## 5. 测试

```sh
cd backend  # 若已在 backend 则省略
uv run pytest
```

单元测试不依赖运行中的 PG、Redis 或 Worker：覆盖健康检查各分支、探针成功/异常/超时、
404、自定义异常、参数错误、未知异常脱敏和任务注册。真实服务连通性须使用上面的命令单独验证。
平台兼容性记录模板见 [docs/PLATFORM_MATRIX.md](docs/PLATFORM_MATRIX.md)。
