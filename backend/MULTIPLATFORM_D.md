# D 阶段实现与服务器验收记录

更新于 2026-11-04。范围为抖音公开取源及必要格式兼容。

## 当前状态

后端已接入抖音专用适配器、精选页和分享链接归一化、原画幅 MP4 档位及报价快照保护。用户样本在境内服务器直连探测中返回 `SOURCE_ACCESS_RESTRICTED`，完整媒体、真实抖音转写与切片尚待验收。阶段 D 保持部分完成状态。[服务器实测报告](DOUYIN_D_SERVER_PROBE.json)

用户确认目前没有抖音解析 API 服务。当前实现采用公开分享页数据和一次受控锁定 SDK 备用。后续商业服务的供应商、文档和调用预算需另行确定。

## 本轮实现

- `jingxuan?modal_id=…`、根路径弹窗、`/video/{id}`、`iesdouyin.com/share/video/{id}` 归一为 `https://www.douyin.com/video/{id}`。分享文本和国内短链复用 A 的入口。重复或冲突 ID、图集、直播和用户主页被拒绝。
- `DouyinSourceBackend` 接入现有 `PlatformSourceBackend` Protocol。元数据、下载和字幕子进程共用注册表。原站与媒体请求继续直连，明确关闭环境和全局代理继承。
- 优先解析公开分享页 `_ROUTER_DATA` 中的当前视频数据，同时兼容单视频 `RENDER_DATA` JSON。只解析 JSON，核对页面与媒体 ID、公开状态、时长、预览/付费/DRM、容器与媒体域名。
- 主分享页最多读取 3MiB，连接/读取使用 5 秒 socket 上限和 8 秒读取预算。主实现与一次 SDK 备用合计最多 12 个 HTTP 请求，受既有 26 秒子进程取源预算和父进程总截止时间控制。SDK 自动重试关闭。明确受限、身份错误、受保护资源和超大响应直接失败。
- SDK 提示需要新鲜匿名 Cookie 时返回脱敏的 `SOURCE_ACCESS_RESTRICTED` 并报告共享冷却所需的限制证据。裸 HTTP 403 保持中性，412 与 429 才分别作为明确限制和限流证据。
- 新增 `video_source_mp4`，显示实际宽高，保留原比例。元数据聚合与执行选流使用同一函数。只有平台模式下的抖音公开合流 MP4 可提供此档位，旧精确高度档位继续按原契约工作。
- 原画幅报价冻结格式 ID、尺寸、编码提示、时长与来源模式，返回源文件最大体积。执行重新提取后核对快照，变化时返回 `SOURCE_SELECTION_CHANGED`。体积上限同时取系统、账号快照和报价源上限的最小值。
- 分享页未明确给出编码时使用 `unknown`，由既有 FFmpeg 本地转换流程处理。媒体传输继续使用受控 HTTP 下载器，FFmpeg 仅读取本地文件。

## 服务器实测

用户提供的样本为 `https://www.douyin.com/jingxuan?modal_id=7700513046734006379`，归一后的 ID 为 `7700513046734006379`。

SSH 使用既有 `videohub-deploy` 账号，UID 1002。探测上传公开源码，在一次性目录中从服务器 uv 缓存加载锁定依赖，直连运行受控提取器并清理临时环境。报告保存源码 SHA256、结果和耗时，诊断输出不回显凭据及签名媒体地址。此次探测只验证元数据，报告中的 `download_verified` 保持 `false`。[完整报告](DOUYIN_D_SERVER_PROBE.json)

预先只读诊断中，分享页返回 HTTP 200 和页面结构，但缺少视频数据，公开详情接口返回 HTTP 403。专用适配器两次探测均返回访问受限，间隔超过 300 秒，最新耗时 0.649 秒。当前样本未进入真实 TaskService/Celery 媒体下载验收。单样本结果尚不足以估计抖音整体成功率。

## 当前能力边界

1. 已有上传业务继续通过现有后端入口处理私有音视频。
2. 用户主动选择下载 MP3 时沿用原有导出流程，公开合流来源可用于此项显式下载。
3. 转写、总结和音频提取的自动音频获取仍要求独立音轨。有界合流抽音的来源模式确认、报价与前端提示未实施，缺独立音轨时继续返回 `AUDIO_STREAM_UNAVAILABLE`。
4. 切片取源预算和定位闭环另待实施，当前服务器 FFmpeg 4.4.2 仍有 `-fps_mode` 参数兼容前置。
5. 原画幅后端接口可通过 Swagger 和 curl 验证。前端原画幅交互尚待专门验收。
6. D 的真实抖音完整下载、取消和多日样本门槛仍待完成。E 的持续实测与发布尚待启动。

## 修改文件

新增内容如下。

- `app/modules/video/parsers/douyin.py`
- `tests/test_douyin_source.py`
- `DOUYIN_D_SERVER_PROBE.json`
- 本交付记录

在 A/B/C 工作区基础上继续修改以下文件。

- `app/modules/video/parsers/{links,dispatch,ytdlp}.py`
- `app/modules/video/{service,resolver}.py`
- `app/modules/download/{presets,service,child}.py`
- `app/modules/billing/{router,service,repository}.py`
- `app/infrastructure/process_runner.py`
- `tests/test_download.py`
- `scripts/bilibili_stage_b_probe.py`，复用为 B/D 的隔离元数据探测入口
- `README.md`、`MULTIPLATFORM_SERVER_PLAN.md` 及 C 交付和审计中的历史哈希说明

## 启动与验证

在 `backend/` 准备现有 PostgreSQL、Redis 和私有配置，执行以下命令。API 和 Worker 采用同一份配置。

```sh
uv sync --locked
VIDEO_EGRESS_MODE=platform uv run python -m app.modules.video.preflight --platform douyin
VIDEO_EGRESS_MODE=platform uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端启动现有 Solo Worker。

```sh
VIDEO_EGRESS_MODE=platform uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

服务器隔离元数据探测可直接通过 SSH 执行。对同一受限样本至少等待既定 300 秒冷却后再作有目的复测，避免频繁重复探测。该 CLI 只执行单次检查，其调用频率由操作员控制。

```sh
uv run python scripts/bilibili_stage_b_probe.py \
  --platform douyin \
  --host videohub-deploy@129.28.26.221 \
  --identity /Users/edgar/.ssh/videohub_actions \
  --python /opt/videohub/.python/cpython-3.14.9-linux-x86_64-gnu/bin/python3.14 \
  --cache /opt/videohub/.cache/uv \
  --url 'https://www.douyin.com/jingxuan?modal_id=7700513046734006379' \
  --report /tmp/douyin-d-metadata.json
```

使用已有登录方式取得测试账号令牌，保留认证与额度，通过 Swagger `/docs` 配置授权或执行以下请求。

```sh
BASE_URL='http://127.0.0.1:8000'
read -r -s TOKEN
curl --fail --max-time 5 "$BASE_URL/api/v1/health/ready"
curl --max-time 40 "$BASE_URL/api/v1/videos/parse" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.douyin.com/jingxuan?modal_id=7700513046734006379"}'
```

本次服务器实测返回 `SOURCE_ACCESS_RESTRICTED`。复测若仍受限，应返回统一错误，进入共享冷却时也可能返回 `SOURCE_COOLDOWN`。只有解析返回实际可用的 `video_source_mp4` 后才继续报价和任务验证。

```sh
curl --fail --max-time 45 "$BASE_URL/api/v1/billing/quotes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.douyin.com/video/7700513046734006379","action":"download","preset_id":"video_source_mp4"}'
QUOTE_ID='实际报价 UUID'
curl --fail --max-time 15 "$BASE_URL/api/v1/billing/tasks" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"quote_id\":\"$QUOTE_ID\"}"
TASK_ID='实际任务 UUID'
curl --fail --max-time 10 "$BASE_URL/api/v1/tasks/$TASK_ID" -H "Authorization: Bearer $TOKEN"
curl -N --max-time 300 "$BASE_URL/api/v1/tasks/$TASK_ID/events" -H "Authorization: Bearer $TOKEN"
```

任务完成后通过 `data.result_payload.file_id` 调用 `/api/v1/files/{file_id}/download`，核对真实尺寸、时长、音视频轨与有界完整解码。另建有预算的任务验证运行中 `POST /api/v1/tasks/{task_id}/cancel`、进程组退出、临时目录清理及终态 CAS。完整步骤也可参照 C 的通用任务验收命令。[认证任务及解码步骤](YOUTUBE_PRODUCTION.md#curl-与-swagger-验证)

## 测试

定向回归执行 `uv run pytest tests/test_douyin_source.py tests/test_download.py tests/test_allowances.py -q`，结果为 **94 passed in 6.56s**。覆盖专用适配器三处调用入口、身份/公开范围、大小与重试预算、SSRF、错误脱敏、原画幅报价与同一任务确认、源变化拒绝，以及真实本地 HTTP 媒体传输和 FFmpeg 对 540×960 竖屏的输出验证。

后端全量 `uv run pytest` 为 **760 passed in 307.90s**。本轮结束时 `git diff --check`、交付文档本地链接及代码块检查通过，服务器报告中 158 个应用源码文件哈希均与本地一致。
