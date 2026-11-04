# 境内服务器的 YouTube 运行与验收

2026-11-04 更新。采用平台模式专用适配器，元数据、下载与字幕共享服务端出口。阶段 C 已通过正式 Linux 服务账号下的单样本隔离 MP3、480p、取消、归属隔离及代理与隔离应用重启后的下载验收。服务器实际变更、文件清单及证据见阶段交付。[C 阶段记录](MULTIPLATFORM_C.md)

## 运行前提与配置

API、Worker、存储、PostgreSQL 和 Redis 沿用现有单机架构。正式服务账号必须可以执行 FFmpeg、Deno >= 2.3，并读取锁文件安装的 yt-dlp 与 EJS。管理员在 API/Worker 私有环境设置以下值。

```dotenv
VIDEO_EGRESS_MODE=platform
YTDLP_PROXY=
YOUTUBE_PROXY=http://USER:PASSWORD@127.0.0.1:17892
YOUTUBE_DNS_MODE=proxy_doh
YTDLP_JS_RUNTIME=deno
YTDLP_DENO_PATH=/正式账号可执行的路径/deno
AUTH_REQUIRED=true
ALLOWANCES_ENABLED=true
STORAGE_MAX_QUOTA_MB=5120
```

代理和 Deno 路径为占位配置，需要按实际账号权限填写。已安装的服务器用户代理在 `videohub-deploy` 下运行，正式应用账号为 `videohub`，应用账号已通过临时隔离运行时、模拟登录及完整媒体任务检查，长期生产路径和配置需随发布准备。当前服务器代理使用从用户现有 Clash 迁移的一条节点，长期可用性依赖该节点与订阅维护。

`YOUTUBE_PROXY` 仅作用于平台模式的 YouTube。B 站、抖音原站按既有平台策略直连。平台模式忽略旧全局 `YTDLP_PROXY`，API 和 Worker 应同时配置及切换。

`YOUTUBE_DNS_MODE` 默认 `system`。当前服务器因系统 DNS 返回受限地址，实测使用 `proxy_doh`，通过 YouTube HTTP 代理访问固定 AliDNS HTTPS 端点，检查全部 A/AAAA 地址。该模式要求 HTTP 代理，保留证书校验和 SSRF 拦截。使用 system 模式时保留原有 SOCKS 代理支持。可信代理端同样需要拒绝私网、回环和云元数据目的地址。

配置加载顺序为 `backend/.env`、根目录 `.env`，进程环境变量优先级最高。容器内回环地址需要按实际网络命名空间核对。配置含凭据，应仅保存在服务器私有配置中。

## 自检与启动

在 `backend/` 执行以下命令。先准备现有数据库迁移、Redis 和正式服务账号的运行环境。

```sh
uv sync --locked
uv run python -m app.modules.video.preflight --platform youtube
uv run python -m app.modules.video.preflight --platform youtube \
  --url 'https://www.youtube.com/watch?v=aqz-KE-bpKQ'
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端启动 Worker。

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

预检实际执行本地 EJS 初始化，并报告 Deno/EJS、出口和 FFmpeg 能力。`passed` 只代表所执行检查通过。完整媒体、取消和正式服务重启需要另外取得证据。

## curl 与 Swagger 验证

以下步骤在正式账号就绪后的隔离验收环境执行。保留 `AUTH_REQUIRED=true` 和 `ALLOWANCES_ENABLED=true`，通过现有登录流程取得测试账号令牌。Swagger `/docs` 配置授权后也可按相同顺序操作。

```sh
BASE_URL='http://127.0.0.1:8000'
read -r -s TOKEN
curl --fail --max-time 5 "$BASE_URL/api/v1/health/ready"
curl --fail --max-time 40 "$BASE_URL/api/v1/videos/parse" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=aqz-KE-bpKQ"}'
```

从实际返回档位中选择，例如 `video_480_mp4`。先查询账号额度，再创建报价，核对返回的资源预算。

```sh
curl --fail --max-time 10 "$BASE_URL/api/v1/billing/allowances" \
  -H "Authorization: Bearer $TOKEN"
curl --fail --max-time 45 "$BASE_URL/api/v1/billing/quotes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=aqz-KE-bpKQ","action":"download","preset_id":"video_480_mp4"}'
```

使用本次返回的 `quote_id` 创建任务。MP3 另用真实可用的 `audio_mp3` 档位重复报价流程，计入现有每日下载与流量限制。

```sh
QUOTE_ID='实际报价 UUID'
curl --fail --max-time 15 "$BASE_URL/api/v1/billing/tasks" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"quote_id\":\"$QUOTE_ID\"}"
TASK_ID='实际任务 UUID'
curl --fail --max-time 10 "$BASE_URL/api/v1/tasks/$TASK_ID" \
  -H "Authorization: Bearer $TOKEN"
curl -N --max-time 300 "$BASE_URL/api/v1/tasks/$TASK_ID/events" \
  -H "Authorization: Bearer $TOKEN"
```

在任务达到 `completed` 后，用 `data.result_payload.file_id` 受控取回结果。对 MP4 和 MP3 分别核对时长、轨道及完整解码，执行时为 FFmpeg 设置外部总超时。

```sh
FILE_ID='实际文件 UUID'
curl --fail --max-time 300 "$BASE_URL/api/v1/files/$FILE_ID/download" \
  -H "Authorization: Bearer $TOKEN" -o /tmp/videohub-youtube.mp4
uv run python - <<'PYCODE'
import subprocess

media = "/tmp/videohub-youtube.mp4"
subprocess.run([
    "ffprobe", "-v", "error", "-show_entries", "format=duration,size",
    "-show_streams", media,
], check=True, timeout=15)
subprocess.run([
    "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", media,
    "-map", "0:v?", "-map", "0:a?", "-f", "null", "-",
], check=True, timeout=180)
PYCODE
```

另建预算允许的测试任务，在运行中调用取消接口，检查最终状态、子进程退出和临时目录清理。测试账号仍受免费每日 2 次下载等限制，跨日安排或使用已授权的测试权益。

```sh
curl --fail --max-time 10 -X POST "$BASE_URL/api/v1/tasks/$TASK_ID/cancel" \
  -H "Authorization: Bearer $TOKEN"
```

重启独立代理后重复认证业务检查，并记录整机重启恢复。字幕需选预先确认有公开字幕的样本，当前 Big Buck Bunny 样本未返回字幕语言。多日样本与发布门槛按总体方案执行。[样本要求](MULTIPLATFORM_SERVER_PLAN.md#102-真实服务器样本)

## 故障分类

| 错误码 | 检查方向 |
| --- | --- |
| `YTDLP_PROXY_INVALID` | 服务端代理格式，proxy_doh 模式的 HTTP 代理要求 |
| `YTDLP_RUNTIME_UNAVAILABLE` | 正式账号 Deno 路径权限、本地 EJS 版本及散列 |
| `SOURCE_NETWORK_UNAVAILABLE` / `DNS_TIMEOUT` / `PARSE_TIMEOUT` | 平台出口、加密 DNS 连通性和请求预算 |
| `SSRF_BLOCKED` | 系统或加密 DNS 返回的受限地址 |
| `SOURCE_ACCESS_RESTRICTED` / `SOURCE_RATE_LIMITED` / `SOURCE_COOLDOWN` | 上游风控、限流及共享冷却 |
| `UNSUPPORTED_URL` | 已删除、私密、直播或其他不支持的来源 |
| `AUDIO_STREAM_UNAVAILABLE` | 缺少独立音轨，可使用文件上传 |

## 验证状态

后端全量回归 **712 passed in 309.42s**。正式 Linux 服务账号 `videohub`（UID 998）在隔离开发模拟登录、认证与额度开启的环境完成 MP3、480p、运行中取消、归属隔离和完成态保护。MP3 为 15,232,868 字节、40.019 秒任务耗时，代理与隔离应用重启后的 480p 为 38,895,635 字节、163.779 秒任务耗时，两者均完整有界解码通过。取消在传输开始后完成，耗时 0.619 秒。[完整报告](YOUTUBE_C_HTTP_CELERY_PROBE.json)

当前使用从既有 Clash 订阅选取的单条严格 TLS AnyTLS 节点。公开字幕、至少三天样本、整机重启、生产登录和长期生产配置仍待验证。应用代码尚未发布，真实切片另有 FFmpeg 4.4.2 的 `-fps_mode` 兼容问题。
