# 阶段 9：微信公众号口令登录与用户任务隔离

## 启动

宿主机 PostgreSQL / Redis 就绪后，从项目根目录分别开三个终端：

```sh
cd backend
uv sync --locked
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

```sh
cd backend
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

```sh
cd frontend
pnpm install --frozen-lockfile
pnpm run dev
```

前端默认连接 `http://localhost:8000/api/v1`。浏览器打开
[工作台](http://127.0.0.1:5173)，接口文档位于 [Swagger](http://127.0.0.1:8000/docs)。
既有 `.env` 无需更改，默认 `AUTH_REQUIRED=False`、`WECHAT_MP_MODE=mock`。
设置 `AUTH_REQUIRED=True` 并重启 API，可要求解析及五类任务创建必须登录并保持关注。

## 本地验收

点击右上角“关注公众号登录”，查看黑白二维码、6 位口令、复制按钮、300 秒倒计时和两步说明。
点击“⚡ 本地模拟关注并发送口令”，最多等待一个轮询周期（1.5 秒），弹窗关闭并展示昵称、“已关注”与“退出”。
模拟模式每个会话使用独立测试 OpenID，方便验证多用户隔离；真实公众号以实际 OpenID 保持身份。
刷新后恢复登录；退出清除 Cookie 与前端 Token，并停止原用户的进度连接、清空其任务和结果视图。

命令行使用 Cookie 也能走完整流程（安装了 Python 3）：

```sh
BASE=http://127.0.0.1:8000/api/v1
SCENE=$(curl --max-time 5 -fsS -X POST "$BASE/auth/wechat/sessions" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["scene_id"])')
curl --max-time 5 -fsS -X POST "$BASE/auth/wechat/sessions/$SCENE/mock-confirm"
curl --max-time 5 -fsS -c /tmp/videohub-cookies.txt "$BASE/auth/wechat/sessions/$SCENE"
curl --max-time 5 -fsS -b /tmp/videohub-cookies.txt "$BASE/auth/me"
curl --max-time 5 -fsS -b /tmp/videohub-cookies.txt "$BASE/tasks"
# 同一会话第二次兑换返回 expired
curl --max-time 5 -fsS "$BASE/auth/wechat/sessions/$SCENE"
curl --max-time 5 -fsS -X POST -b /tmp/videohub-cookies.txt -c /tmp/videohub-cookies.txt "$BASE/auth/logout"
```

Swagger 的 auth 分组可创建会话、模拟确认、兑换和查询 me。同源 Cookie 自动用于其他接口。
取得真实任务 UUID 后，可验证详情、SSE 和取消（带所属用户的 Cookie）：

```sh
TASK_ID='替换为 task_id'
curl --max-time 5 -fsS -b /tmp/videohub-cookies.txt "$BASE/tasks/$TASK_ID"
curl -N --max-time 310 -b /tmp/videohub-cookies.txt "$BASE/tasks/$TASK_ID/events"
curl --max-time 5 -fsS -X POST -b /tmp/videohub-cookies.txt "$BASE/tasks/$TASK_ID/cancel"
```

也支持 `Authorization: Bearer <access_token>`，优先于 Cookie。
仅 `/tasks/{id}/events` 与 `/files/{id}/download` 接受 `?token=...`。
前端原生 EventSource 配置 `withCredentials` 并在有 Token 时附带 Query；下载链接同样处理。
CORS 仅允许配置的明确源，并放行 `Authorization` 与凭据。

## 真实个人公众号

在服务端环境配置以下字段，API 重启生效：

```dotenv
APP_ENV=production
AUTH_REQUIRED=True
JWT_SECRET_KEY=使用独立且足够长的随机密钥
WECHAT_MP_MODE=verify_code
WECHAT_MP_TOKEN=与公众号后台一致的Token
WECHAT_MP_STATIC_QR_URL=https://你的公开域名/公众号固定二维码.png
```

把公众号服务器 URL 指向公网 HTTPS 的 `/api/v1/auth/wechat/webhook`，Token 保持一致。
当前支持签名校验的明文 XML 回调；公众号后台选择明文模式。未实现加密回调。
用户扫码关注后，向公众号聊天框发送网页显示的 6 位口令。Token 与 AppSecret 仅保存在服务端。
生产 Cookie 使用 Secure，浏览器须通过 HTTPS 访问；建议前后端同站部署。
固定二维码未配置时返回内置 SVG 演示二维码，不能代替真实公众号二维码。

具备权限的公众号可使用 `parameterized_qr`，配置 `WECHAT_MP_APP_ID`、`WECHAT_MP_APP_SECRET`。
适配器请求微信官方临时二维码，处理 `subscribe/qrscene_...` 与已关注用户的 `SCAN`。
网络连接上限 2 秒、单次请求上限 4 秒、至多两次尝试、整体上限 12 秒；普通个人号优先使用 `verify_code`。
开发环境或 `mock` 模式允许模拟确认；生产非 mock 模式返回 403。生产未替换默认 JWT 密钥时拒绝签发凭证。
未使用真实公众号凭据调用官方接口，真实微信联调需要部署回调和配置公众号固定二维码。

## 数据与安全行为

- 新建 `users`，`tasks.user_id` 可空且带索引与外键；迁移 `0004` 已执行，历史匿名任务仍可凭 UUID 访问。
- 口令通过 Redis `SET NX EX 300` 分配，全局有效期内唯一；最多尝试 30 次，过载明确返回 503。
- 微信消息安全 XML 解析禁用 DTD/实体，消息体最大 64 KiB。验签失败 403。
- MsgId 去重 60 秒；事件缺少 MsgId 时使用 `openid_CreateTime`。失败会释放去重键，允许微信重发恢复。
- 错误口令 5 分钟内达 5 次，后续尝试被限流；成功登录清零。非口令普通消息始终纯文本 `success`。
- 会话认证通过 Lua 原子检查 pending、绑定用户并删除口令；并发轮询通过 Lua 单次删除已认证会话。
  只会签发一次 JWT；客户端在成功响应丢失时需重新打开登录弹窗。
- 取关写数据库与 Redis 标记。每次鉴权以数据库为准，即使 Redis 标记丢失或过期也不能复活旧 Token。
  已连接 SSE 每次监控循环重新检查用户关注状态，失效时发送 error 并关闭。
- JWT 固定 HS256、检查签名/过期时间/签发者/受众；停用用户同样拒绝访问。
- 通过请求上下文将用户绑定至统一 TaskService，任务持久化归属后才入队。未登录默认仍可创建匿名任务。
- 详情、取消、SSE 校验归属。历史保留原分页结构：匿名 `items=[]`，登录后只查询自己的记录。
- 文件、文稿与 AI 结果均验证生成任务归属；共享缓存仅允许持有已完成任务引用的用户读取。
  AI 任务的授权同时覆盖其结果所关联的确切源文稿，避免通过结果 UUID 绕过任务隔离。
- 登出清除浏览器凭据；已复制的 JWT 保留原有效期。取关和停用会使其后续鉴权失败。
- 前端采用项目已有 React 与订阅式 Store，新增 `.tsx` 弹窗与 `stores/auth.ts`；保持既有技术栈。

## 检查命令

```sh
cd backend
uv run pytest
uv run alembic current
uv run alembic check
```

```sh
cd frontend
pnpm run type-check
pnpm run build
pnpm run test:unit
```

后端专项测试 `tests/test_wechat_auth.py` 使用隔离 Redis Unix socket 与临时 SQLite，
覆盖签名/XML/XXE、幂等、口令冲突/TTL/限流、并发认证/兑换、关注闭环、Header/Cookie/Query、
多用户越权、创建归属/强制鉴权、文件/文稿/AI 共享缓存授权、SSE 连接内失效及依赖故障恢复。
前端 `src/tests/auth.test.tsx` 覆盖弹窗、复制、倒计时、模拟确认、轮询成功/过期、清理、凭据注入、账户切换与旧请求失效竞态。
既有历史 API 测试仅改为使用登录用户及其任务，保留分页、排序、标题等断言。

## 修改文件清单

新增：

- `backend/alembic/versions/0004_wechat_users_and_task_ownership.py`
- `backend/app/modules/auth/{__init__,models,repository,tokens,qr,service,dependencies,router}.py`
- `backend/tests/test_wechat_auth.py`、`backend/STAGE9.md`
- `frontend/src/stores/auth.ts`、`frontend/src/components/auth/WeChatLoginModal.tsx`
- `frontend/src/tests/auth.test.tsx`

修改：

- `.env.example`、`.env.docker.example`、`backend/README.md`
- `backend/pyproject.toml`、`backend/uv.lock`
- `backend/app/core/config.py`、`backend/app/main.py`、`backend/app/api/v1/router.py`
- `backend/app/infrastructure/database/models.py`
- `backend/app/modules/tasks/{models,repository,service,history,router}.py`
- `backend/app/modules/{video,download,media,transcript,ai}/router.py`
- `backend/app/modules/ai/{repository,service}.py`、`backend/app/modules/transcript/service.py`
- `backend/tests/test_history_and_security.py`
- `frontend/src/App.tsx`、`frontend/src/api/client.ts`、`frontend/src/components/AppHeader.tsx`
- `frontend/src/lib/taskTracker.ts`、`frontend/src/stores/workspace.ts`
- `frontend/src/hooks/useWorkspaceResults.ts`、`frontend/src/style.css`
- `frontend/src/tests/reactApp.test.tsx`

## 本次执行结果

- PostgreSQL 已升级至 `0004 (head)`，`uv run alembic check` 无新增迁移差异。
- `uv run pytest`：210 passed（包含 193 项存量测试及 17 项阶段 9 测试）。
- `pnpm run type-check` 与 `pnpm run build`：通过。
- `pnpm run test:unit`：40 passed。
- 真实本地浏览器验收：弹窗、模拟登录、关注徽章、刷新恢复与退出均通过。
- `git diff --check`：通过。
