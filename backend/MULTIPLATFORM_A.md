# 三平台取源改造：里程碑 A 交付

2026-11-03，分支 `codex/multiplatform-a`。本阶段完成平台入口、出口隔离与跨 API/Worker 的取源协调；本地改动尚未推送、合并或部署，生产配置未变更。B–E 仍待实施。

## 实际行为

- 分享文本只接受一个 HTTP(S) URL，剥离已知追踪参数，保留 B 站分 P；YouTube watch/shorts/embed/live/youtu.be 统一到视频 ID。
- platform 模式下，国内短链直连还原，最多五次重定向、八秒总预算，逐跳及连接时校验公网地址，只读响应头。
- 元数据、下载、字幕三个子进程共用 `PlatformSourceBackend` Protocol 与平台注册表。音频提取、切片取源通过原下载适配器继承同一出口策略。
- **三个平台注册项目前均使用现有受保护的 yt-dlp 内核。没有新增 B 站 WBI 专用取源、抖音签名服务或商业解析 API。**
- 父进程使用现有 Redis 协调，每平台/策略版本同时最多一个元数据提取。下载、字幕身份验证后通过私有事件提前释放元数据锁，长传输仍受原 Watchdog 保护。私有事件不进入进度、SSE 或结果。
- 默认 60 秒内连续三次明确 412、Bot/验证码或 429 后冷却 300 秒。普通 403、取消和资源错误不据此熔断。源站成功清零计数，缓存命中和短链还原不冒充恢复。
- 半开只放行一个带 TTL 的探测；结果登记幂等，旧代结果不能覆盖新代状态。Redis 不可用时新增取源返回 `DEPENDENCY_UNAVAILABLE`。
- 沿用公开响应、任务编排、档位、账务、存储、上传归属与既有成果缓存；没有数据库迁移或依赖变更。

## 配置与切换

三个环境模板默认保持 `VIDEO_EGRESS_MODE=legacy`，兼容现有全局出口。启用新模式时 API/Worker 必须使用同一配置：

```dotenv
VIDEO_EGRESS_MODE=platform
VIDEO_EGRESS_POLICY_VERSION=v1
YOUTUBE_PROXY=
SOURCE_FAILURE_THRESHOLD=3
SOURCE_FAILURE_WINDOW_SECONDS=60
SOURCE_COOLDOWN_SECONDS=300
```

platform 模式：B 站、抖音及其他兼容平台显式直连，忽略环境代理和旧 `YTDLP_PROXY`；仅 YouTube 使用管理员私有配置中的 `YOUTUBE_PROXY`。缺出口返回 `SOURCE_EGRESS_UNAVAILABLE`，不回退到全局代理。Deno/EJS 配置检查只影响 YouTube，国内平台不受无效 YouTube 配置牵连。

代理只能由运维配置，不能由用户请求传入。沿用 `YTDLP_JS_RUNTIME=deno` 和 `YTDLP_DENO_PATH`，不自动安装远程组件。密钥和代理凭据不进入仓库。

新变量不会自动创建服务器独立出口。Mac Clash 临时隧道尚未替换；生产迁移应在平台独立验收后，按维护窗口收尾旧任务、统一切换 API/Worker、再退出临时出口。回滚须统一恢复旧模式与相应私有配置。

## 启动与预检

准备 PostgreSQL、Redis、FFmpeg 和开发私有配置，在 `backend/` 启动：

```sh
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另一终端也进入 `backend/`，读取同一配置：

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

以实际服务账户执行，以下无 URL 的检查不访问视频平台：

```sh
uv run python -m app.modules.video.preflight --platform all
uv run python -m app.modules.video.preflight --platform bilibili
uv run python -m app.modules.video.preflight --platform youtube
uv run python -m app.modules.video.preflight --platform douyin
```

以下进行一次有界元数据提取，不下载媒体：

```sh
uv run python -m app.modules.video.preflight --platform bilibili \
  --url 'https://www.bilibili.com/video/BV1g2JX6nESP?t=3.6'
```

`--platform` 必须匹配 URL；`all` 不能与 URL 混用。配置通过不代表上游成功；输出分别标记 `metadata_verified`、`download_verified`、FFmpeg `render_verified`，后两项不会因标题成功而通过。

预检新增 `fps_mode` / `vsync` 参数能力报告。已知服务器 FFmpeg 4.4.2 不支持现有切片命令中的 `-fps_mode`；本阶段只报告问题，没有替换渲染参数或升级服务器。

## curl / Swagger 验证步骤

以下是交付后的操作说明，本轮未访问生产或触发真实收费任务。

```sh
curl --fail --max-time 5 http://127.0.0.1:8000/api/v1/health/live
curl --fail --max-time 5 http://127.0.0.1:8000/api/v1/health/ready
curl --max-time 40 http://127.0.0.1:8000/api/v1/videos/parse \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"分享 https://www.bilibili.com/video/BV1g2JX6nESP?t=3.6 。"}'
```

`$TOKEN` 使用真实测试账号令牌。也可在 `/docs` 授权后：

1. 提交一条分享文本、两个 URL、B 站 `p=2` 链接，检查单链接进入解析、多链接拒绝、分 P 保留；上游错误仍有统一 `error` 包装和 `request_id`。
2. 仅在开发环境清空 `YOUTUBE_PROXY`，新 YouTube 取源应明确失败；国内平台不受该配置错误影响。
3. 有真实预算时通过 `billing/quotes` → 用户确认 → `billing/tasks` 创建下载或转写，再核对 `tasks/{id}`、SSE、取消、受控文件获取。原字幕优先和音频回退流程继续保留。
4. 上传仍通过 `uploads` → 文件内容 → complete → `media_probe` 进入后续流程。冷却时前端提供上传入口；未新增 SRT 导入。

冷却和跨进程行为通过隔离测试验证，不为人工验收高频触发平台风控。完整媒体服务器实测按总方案 E 的预登记样本和账号/磁盘预算执行。

## 验证结果

最终版本 `uv run pytest`：**607 passed，303.74 秒**。此前首轮全量 603 项通过；补齐边界测试后再次执行最终全量，607 项全部通过。

另外通过：三平台专项 50 项；前端 29 个文件、346 项；`pnpm type-check`；`VITE_API_BASE_URL=/api/v1 VITE_CLIPS_ENABLED=true pnpm build` 及公开产物校验；`git diff --check` 和方案/交付文档本地链接检查。

专项使用临时 Redis、ASGI 客户端及真实 Python 子进程，覆盖 API/Worker 双向冷却、独立进程共享状态、半开/迟到/重复报告、缓存读取、私有事件过滤、元数据超时清理、下载/字幕成功回调提前解锁和短链规范身份持久化。离线假数据不计为真实平台下载验收。

```sh
# backend/
uv run pytest
uv run pytest tests/test_multiplatform.py tests/test_source_operations.py -q
# frontend/
pnpm type-check
pnpm test:unit
VITE_API_BASE_URL=/api/v1 VITE_CLIPS_ENABLED=true pnpm build
```

## 修改文件清单

以下相对仓库根目录；同一单元格中省略路径的文件与前一文件同目录：

| 范围 | 文件 |
| --- | --- |
| 配置 | `.env.example`、`.env.docker.example`、`backend/deploy/production.env.example`、`backend/app/core/config.py` |
| 新增平台基础 | `backend/app/modules/video/parsers/links.py`、`dispatch.py`、`operations.py` |
| 解析与缓存 | `backend/app/modules/video/parsers/runtime.py`、`ytdlp.py`、`ytdlp_worker.py`、`cached.py` |
| 服务与预检 | `backend/app/modules/video/resolver.py`、`service.py`、`preflight.py` |
| 子进程与取源 | `backend/app/infrastructure/process_runner.py`、`backend/app/modules/download/adapter.py`、`child.py`、`backend/app/modules/transcript/subtitles.py`、`subtitle_child.py` |
| 后端测试 | `backend/tests/conftest.py`、`test_multiplatform.py`、`test_source_operations.py` |
| 前端交互 | `frontend/src/utils/videoLink.ts`、`errorPresentation.ts`、`frontend/src/views/HomeView.tsx` |
| 前端测试 | `frontend/src/tests/videoLink.test.ts`、`reactApp.test.tsx` |
| 文档 | `backend/README.md`、`MULTIPLATFORM_SERVER_PLAN.md`、`MULTIPLATFORM_A.md` |

## 阶段停止条件

阶段 A 到此停止。B 才实施 B 站专用取源及境内直连验证；C 解决 YouTube 独立出口；D 解决抖音取源、合流抽音预算和竖屏格式；E 验证真实样本并逐平台发布。

三平台完整下载成功率、Mac 关机后的服务器独立运行、生产 FFmpeg 成片兼容性和真实时间戳定位均未验收。现有千问 ASR 配置未调整，不能据此宣称精准切片已修复。
