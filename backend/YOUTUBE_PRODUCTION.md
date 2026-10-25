# 国内服务器部署 YouTube 下载

用户粘贴公开视频链接后，由服务器解析并通过 Celery 下载，浏览器最后从网站下载结果文件。用户自己的 VPN 不会给服务器提供出口；这套方案由管理员配置服务器出口，用户无需填写代理或 Cookie。

## 部署前提

V1 建议 API、Worker 与文件存储在同一台服务器，沿用现有 PostgreSQL / Redis / Celery 架构。管理员需要先准备能访问 YouTube 及其媒体域名的稳定出口代理。当前代码没有购买、开通或部署出口服务。

API 的元数据解析、Worker 的视频/音频下载及字幕获取必须使用同一套出口配置。仅给 Worker 配代理会导致 API 解析仍失败。若之后迁移到境外 Worker，还需解决 API 元数据解析出口和共享媒体存储；本次没有实现跨服务器文件传输。

## 安装与配置

在 API 和 Worker 的运行环境安装 FFmpeg，以及 **Deno >= 2.3.0**。从 [Deno 官方安装文档](https://docs.deno.com/runtime/getting_started/installation/) 选择对应系统的安装方式，确认服务运行账户可以执行它。

在 `backend/` 执行：

```sh
uv sync --locked
```

锁文件包含 `yt-dlp[default]` 与匹配的本地 `yt-dlp-ejs`。不启用远程组件下载，不接受用户上传 Cookie 或自定义代理。

在管理员维护的 `.env` 中设置（替换占位符，勿提交真实凭据）：

```dotenv
YTDLP_PROXY=http://proxy.example.com:8080
YTDLP_JS_RUNTIME=deno
YTDLP_DENO_PATH=/usr/local/bin/deno
```

`YTDLP_DENO_PATH` 可留空以从服务的 PATH 查找 Deno；默认运行时为 `disabled`，生产部署需显式启用。代理支持 HTTP、SOCKS4/4a、SOCKS5/5h；账户密码中的特殊字符需按 URL 编码。代理值按密钥类型读取，错误响应不回显代理地址或密码。

配置加载顺序为 `backend/.env`、项目根目录 `.env`，后者覆盖前者；进程环境变量优先级最高。API 和 Worker 更新配置后都要重启。`127.0.0.1:7890` 仅适用于同一网络命名空间里确实运行该代理的环境；容器内的回环地址指向容器自身。

服务器 DNS 必须返回真实公网地址。代理不会跳过源站 DNS / SSRF 校验；出现 `SSRF_BLOCKED` 时应修复服务器解析。可信代理端也必须限制私网、回环和云元数据目的地址，防止代理侧 DNS 解析与应用侧不一致造成访问越界。保持 TLS 校验与 SSRF 防护开启。

## 自检与启动

以下命令均在 `backend/` 执行；先启动 PostgreSQL / Redis 并完成现有数据库迁移。

```sh
uv run python -m app.modules.video.preflight
uv run python -m app.modules.video.preflight --url https://youtu.be/ZePPpQLAhcU
```

第一条检查配置、Deno/EJS 与 FFmpeg；第二条增加真实元数据解析。命令使用短版本探测超时与现有解析超时，失败返回非零退出码。`passed: true` 仅表示所执行的检查通过。`metadata_verified: true` 也不代表下载成功；`download_verified` 始终为 false，下载须单独验收。

分别启动 API 与 Worker（生产由进程管理器托管，API 放在 HTTPS 反向代理后）：

```sh
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

保留现有总超时、有限重试、进程组取消、磁盘配额和文件过期清理。Deno 由受限的官方 yt-dlp provider 启动，归属现有受监控子进程组。

## 真实下载验收

启用认证时，先通过现有公众号登录流程获取当前用户令牌；Swagger `/docs` 中配置授权后可按下列顺序验证。也可在终端设置以下变量（令牌不写入文档或版本库）：

```sh
BASE_URL='http://127.0.0.1:8000'
read -s TOKEN
```

1. 解析公开视频，确认 `data.download_options` 非空：

   ```sh
   curl --fail --max-time 40 "$BASE_URL/api/v1/videos/parse" \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"url":"https://youtu.be/ZePPpQLAhcU"}'
   ```

2. 从实际返回的档位中选择 `preset_id`，下例仅在确实返回 `video_480_mp4` 时使用：

   ```sh
   curl --fail --max-time 45 "$BASE_URL/api/v1/downloads" \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"url":"https://youtu.be/ZePPpQLAhcU","preset_id":"video_480_mp4"}'
   ```

3. 将返回的任务 ID 填入变量，轮询任务或在已登录的网页订阅 SSE，直到成功终态：

   ```sh
   TASK_ID='实际任务 UUID'
   curl --fail --max-time 10 "$BASE_URL/api/v1/tasks/$TASK_ID" \
     -H "Authorization: Bearer $TOKEN"
   ```

4. 使用 `data.result_payload.file_id` 获取文件，并验证其可解码：

   ```sh
   FILE_ID='实际文件 UUID'
   curl --fail --max-time 120 "$BASE_URL/api/v1/files/$FILE_ID/download" \
     -H "Authorization: Bearer $TOKEN" -o /tmp/videohub-youtube.mp4
   ffprobe -v error -show_entries format=duration,size -show_streams /tmp/videohub-youtube.mp4
   ```

还应通过网页验证 MP3、字幕获取、取消任务、任务归属隔离，以及未登录/取关后的访问限制。浏览器拿到文件、FFprobe 确认媒体有效才算真实下载通过；元数据与模拟测试不能替代这一步。

## 错误排查与交付状态

| 错误码 | 运维检查 |
| --- | --- |
| `YTDLP_PROXY_INVALID` | 检查代理 URL 格式、协议、端口及凭据编码 |
| `YTDLP_RUNTIME_UNAVAILABLE` | 检查 Deno >= 2.3、服务账户 PATH、本地 EJS 与依赖锁文件 |
| `SOURCE_NETWORK_UNAVAILABLE` / `PARSE_TIMEOUT` | 检查 API 和 Worker 的出口、DNS、代理连通性 |
| `SSRF_BLOCKED` | 检查源站 DNS 是否返回非公网地址，保持防护开启 |
| `SOURCE_ACCESS_RESTRICTED` | 上游要求人机验证或拒绝当前服务器出口 |
| `SOURCE_RATE_LIMITED` | 降低并发、等待限流解除，检查出口负载 |

网络连通不能保证所有视频可下载；登录、年龄、地区、版权与平台风控限制仍然存在。当前仅支持公开免登录资源。

本次修改范围：两份环境模板、`app/core/config.py`、`app/modules/video/parsers/{runtime,ytdlp,ytdlp_worker}.py`、`app/modules/video/preflight.py`、`app/modules/download/child.py`、`app/modules/transcript/subtitle_child.py`、`app/infrastructure/process_runner.py`、`pyproject.toml`、`uv.lock`、`tests/test_youtube_server_config.py` 及本部署文档/README。

本地测试：`AUTH_REQUIRED=False WECHAT_MP_STATIC_QR_URL= uv run pytest`（隔离开发机 `.env` 的认证开关与自定义二维码配置）。真实服务器出口尚未提供；尚未完成生产环境 YouTube 下载验收。
