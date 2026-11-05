# 抖音匿名浏览器取源

更新于 2026-11-05。

## 实现与运行条件

`DOUYIN_SOURCE_BACKEND=browser` 使用 Playwright 1.64.1 和匹配的 Chromium 获取当前公开视频的详情响应，随后由现有受控下载器传输媒体。来源注册表同时覆盖元数据、下载和字幕获取入口。默认配置为 `native`，操作员在完成隔离验收后设置浏览器运行时路径。

Linux 浏览器使用无特权用户、网络、PID 和挂载命名空间。命名空间内只有回环网络，HTTPS 流量经 Unix socket 转发到宿主机受控出口。出口检查全部 DNS 地址并连接已经验证的公网 IP。新命名空间拥有独立的 128MiB `/tmp`，浏览器启用 Chromium sandbox。浏览器环境只包含运行所需变量。

取源总预算为 23 秒，HTTPS 连接最多 128 次、同时最多 64 路、合计最多 64MiB，单个详情响应最多 3MiB。外层继续使用元数据截止时间、任务取消、进程组 Watchdog 和磁盘检查。PID 命名空间随主进程退出而销毁，覆盖 Chromium 创建独立进程组的情况。临时来源数据保存在元数据父进程管理的目录或任务私有目录，取消后随对应目录清理。

浏览器需要 Linux 的 `unshare`、`mount`、`ip` 和可用的非特权用户命名空间。私有 `/tmp` 挂载后，Python 和 Playwright Node 驱动的临时目录同步重设为 `/tmp`。组合验收服务采用 `TasksMax=512`、`MemoryMax=2G`、`CPUQuota=150%`，128 个进程与线程的旧额度会导致 Chromium 或转发线程启动失败。

浏览器从单视频页面的详情响应提取与 `aweme_id` 匹配的数据，继续检查公开状态、完整时长及受保护资源。只处理匿名访问。验证码作为受限结果返回并进入已有平台冷却。浏览器来源使用独立的冷却键，元数据缓存包含浏览器配置版本。

抖音详情可能同时提供合流 MP4、DASH 画面流和原始独立音轨。适配器按每个格式的 `format` 字段区分画面流，从 `video.bit_rate_audio` 获取原始音轨。音频提取继续优先独立音轨，背景音乐字段不参与原始音频选择。浏览器来源的格式 ID 使用稳定格式属性，避免列表重排导致报价误判。

播放地址元数据的 `url_key` 明确标注 `_h264_` 且编码未标记为 HEVC 时，适配器识别为 H.264，现有下载器可直接复制视频轨并处理音轨。缺少明确标识时继续使用原有转码策略。该路径有专门回归测试，完整媒体验收使用独立的新候选目录保留结果。

格式标识只接收数值码率，默认流中的 `bit_rate` 子列表不参与身份计算。这样同级格式播放地址刷新或列表重排时，默认流的标识仍然稳定。

仅合流且缺少独立音轨的来源继续需要有界抽音与显式报价确认。此类来源的自动转写能力需单独验收。

## 服务器初步证据

用户样本 `7700513046734006379` 已通过服务器匿名取源。首次有效取源耗时 5.790 秒，增加 PID 隔离及私有临时文件系统后的取源耗时约 6 秒。页面返回对应详情，登录要求及验证码检测均为否，网络命名空间直接外连测试被拒绝。

单样本已完整下载 1280×720 HEVC 画面流和 AAC 原始音轨，分别为 8,305,556 与 12,376,021 字节。两路时长分别为 511.533333 与 511.535442 秒。本地 FFmpeg 合并并完成整文件解码，耗时 118.924 秒。原始播放地址和匿名页面数据保留在服务器私有测试目录。

正式 `videohub` 账号的认证 API 验收也已交付此样本的 MP3 和 MP4，分别为 12,278,795 与 40,203,712 字节，均完成整文件解码。MP3 使用原始独立音轨，API 链路耗时分别为 18.721 与 150.612 秒。浏览器运行中取消用时 1.17 秒，进程与临时目录检查通过，跨账号访问和完成状态保护通过。

## 多样本验收

`acceptance-douyin-browser-samples.json` 预登记 20 个候选，其中一个为用户原链接，其余来自公开抖音页面索引，覆盖科普、摄影、生活和美食。实际可用性由服务器测试确认。全部结果保留，包括受限、删除、不支持、超时和冷却结果。

验收使用独立 schema、队列、Redis 数据库、存储和测试账号，通过认证 API、报价、TaskService、Celery Solo 和受控文件接口执行。账号权益使用此前授权的管理员发放流程并记录审计。媒体必须通过大小、时长、音视频轨、画幅及完整解码检查。另在浏览器运行时取消任务，验证进程退出及临时目录清理。浏览器响应数与 urllib HTTP 请求数分别记录。

测试关闭 ASR，LLM 使用 heuristic。公开样本通过完整音视频交付后，再决定试运行启用范围。

首轮扩展测试由部署账号 UID 1002 执行，20 个固定样本中 18 个通过正常下载子程序及整文件解码。一个 2566.966 秒的视频超出本轮 1200 秒预算，元数据解析成功，媒体下载按预算跳过。另一个样本触发 `SOURCE_SELECTION_CHANGED`，原始记录保留，单独复测。两个已验证的 H.264 样本分别输出 1280×720 和 1080×1920 的 MP4，耗时 33.737 与 34.894 秒。

上述扩展测试采用操作员探测，正式账号的认证、报价与任务编排证据来自前述 API 验收。完整 20 样本的正式账号复测仍待完成。浏览器取源处于隔离试验阶段，长期观察结果单独记录。

## 配置与启动

浏览器运行时和操作系统依赖由服务器管理员安装到 root 持有的版本目录。部署使用锁定 Python 依赖，Chromium 版本与 Playwright 匹配。当前经校验的服务器运行时配置如下。

```sh
VIDEO_EGRESS_MODE=platform
DOUYIN_SOURCE_BACKEND=browser
DOUYIN_BROWSER_EXECUTABLE_PATH=/opt/videohub/runtime/douyin/699ec1bd724ce85d/chromium/chrome-linux64/chrome
DOUYIN_BROWSER_LIBRARY_PATH=/opt/videohub/runtime/douyin/699ec1bd724ce85d/libs/usr/lib/x86_64-linux-gnu
```

在 `backend/` 使用同一份配置启动 API 和 Solo Worker。

```sh
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
uv run pytest
```

Linux 操作员可运行独立取源探测，目录必须为当前账号私有目录。报告只包含状态与统计，原始来源数据写入私有目录。

```sh
uv run python scripts/douyin_browser_probe.py run \
  --directory /tmp/videohub-douyin-private-probe \
  --video-id 7700513046734006379 \
  --report /tmp/videohub-douyin-probe.json
```

在 `/docs` 使用已有账号登录，再调用解析和报价接口。令牌通过私有环境传入。

```sh
curl --fail --max-time 35 https://vedio.vip/api/v1/videos/parse \
  -H "Authorization: Bearer $VIDEOHUB_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.douyin.com/video/7700513046734006379"}'
```

选择实际返回的档位，通过 `POST /api/v1/billing/quotes`、`POST /api/v1/billing/tasks`、`GET /api/v1/tasks/{task_id}` 和 `GET /api/v1/files/{file_id}/download` 完成完整文件验证。

## 修改文件

- `app/modules/video/parsers/douyin_browser.py` 和 `scripts/douyin_browser_probe.py`，隔离浏览器取源与操作员探测。
- `app/modules/video/parsers/douyin.py`，来源接入、DASH 与原始音轨识别。
- `app/core/config.py`、配置模板及 `pyproject.toml`、`uv.lock`，配置与锁定依赖。
- `app/modules/video/parsers/{cached,operations,ytdlp}.py`，缓存、冷却、元数据临时目录与错误映射。
- `app/modules/download/child.py`、`app/modules/transcript/subtitle_child.py` 和 `app/infrastructure/process_runner.py`，任务目录与错误传播。
- `deploy/prepare_acceptance_e.py`、`scripts/acceptance_e.py`、`scripts/acceptance_e_trace/sitecustomize.py` 和样本清单，独立真实服务器验收。
- `tests/test_douyin_browser.py`、`tests/test_stage_e.py` 及测试环境配置，网络边界、格式选择和运行库复制回归。
