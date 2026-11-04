# C 阶段交付与验收记录

更新于 2026-11-04。里程碑为 YouTube 服务端独立运行。

## 当前结果

专用适配器、离线运行时检查、按平台出口和可选加密 DNS 已实现。服务器独立代理已运行。2026-11-04 在正式 Linux 服务账号 `videohub`（UID 998）下完成单样本隔离 HTTP → TaskService → Celery Solo 验收，完整 MP3、480p MP4、运行中取消、归属隔离和完成态保护均通过。重启服务器代理并重新创建隔离 API/Worker 后，480p 下载通过。[完整媒体验收报告](YOUTUBE_C_HTTP_CELERY_PROBE.json)

验收使用隔离开发模拟登录，认证与额度保持开启，ASR 关闭且无付费模型调用。后端全量 `uv run pytest` 为 **712 passed in 309.42s**。公开字幕、多日样本、整机重启和生产发布仍待完成。应用代码位于本地 `codex/multiplatform-c`，尚未提交、推送、合并或发布。

## 实现与检查中的修正

- `YoutubeSourceBackend` 通过现有 `PlatformSourceBackend` Protocol 注册。元数据、下载和字幕子进程使用同一实现。验证请求与返回的视频身份，拒绝播放列表、直播、登录限制及 DRM，过滤故事板和不支持的容器。
- 单次取源使用 26 秒预算、最多 32 次 SDK 请求，独立 DNS 查询另有最多 32 次上限。关闭平台模式 YouTube 的 SDK 自动重试及适配器备用。完整媒体传输继续由现有任务总超时与 watchdog 约束。
- 平台模式通过 `YOUTUBE_PROXY` 配置出口。B 站、抖音保持直接访问的既定策略。MP3 必须具有独立音轨，合流资源仍可提供符合条件的视频档位。
- Deno/EJS 预检通过 `YoutubeRuntimeCapabilities` Protocol。核对锁定 SDK 所认可的本地 EJS 版本和 SHA3-512，实际执行离线初始化，禁止远程脚本、npm 和网络权限。脚本初始化顺序已与官方 provider 对齐。
- 服务器系统 DNS 曾返回 YouTube 的受限地址 `2001::1`。新增默认关闭的 `YOUTUBE_DNS_MODE=proxy_doh`，通过该平台的 HTTP 代理访问固定 AliDNS HTTPS 端点。全部 A/AAAA 地址继续执行公网校验，TLS 校验开启。单次查询组限制 8 秒，单响应限制 64KiB，禁止重定向与重试。
- DNS 以 `PublicDnsResolver` Protocol 注入受控子进程，使用锁与短期缓存，退出恢复原 socket resolver。父进程异步查询可取消。加密 DNS 模式要求 HTTP 代理，system 模式保留原有 SOCKS 支持。
- 旧测试视频 `BaW_jenozKc` 实际返回视频不可用。已将该类错误归为 `UNSUPPORTED_URL`，避免把内容删除误记为平台风控及触发冷却。
- `preflight` 分别报告配置、元数据和完整业务验收状态。元数据通过后仍保留 `download_verified=false`、`standalone_verified=false`、`restart_verified=false`。

## 服务器实际变更

在既有 `videohub-deploy` 账号部署了 Mihomo 1.19.33 和 Deno 2.9.8。官方发布地址及压缩包 SHA256 见运行时清单，服务器二进制 SHA256 核对通过。[运行时清单](deploy/youtube/runtime-artifacts.json)

从用户现有 Mac Clash/Mihomo 配置中先迁移一条 VLESS 节点，后经验证选用同一现有订阅中的一条 AnyTLS 节点。服务器保存最小节点配置，节点可用性和费用仍取决于原有服务商与订阅。此次选择单节点用户服务，后续需要维护节点轮换。核对服务器二进制后，已清理本次创建的本地私有暂存目录。

服务器安装了 `videohub-youtube-proxy.service` 用户服务，已启用并运行，`videohub-deploy` 的 Linger 已开启。监听仅限 `127.0.0.1:17892`，本地代理启用认证，关闭 TUN 和外部控制器，保留证书校验及私网拒绝规则。内存上限 256MiB，任务数上限 64。[用户服务模板](deploy/youtube/videohub-youtube-proxy.user.service)

| 服务器文件 | 权限 | 用途 |
| --- | --- | --- |
| `~/.local/lib/videohub-youtube/{mihomo,deno}` | 0700 | 部署账号私有二进制 |
| `~/.config/videohub-youtube/proxy.yaml` | 0600 | 最小节点及本地代理配置 |
| `~/.config/videohub-youtube/client.json` | 0600 | 本地代理客户端配置与 DNS 模式 |
| `~/.config/videohub-youtube/verified-binaries.json` | 0600 | 安装校验记录 |
| `~/.config/systemd/user/videohub-youtube-proxy.service` | 用户服务 | 进程托管 |

仓库还提供可选的系统级 DynamicUser 模板，当前服务器实际使用用户服务模板。模板位于 `deploy/youtube/`，避免被原有部署脚本的顶层 service glob 意外安装。

正式 API/Worker 使用 `videohub` 账号，其权限无法直接使用部署账号的私有二进制和客户端文件。隔离验收为该账号创建临时 Deno 与私有配置，已完成运行时、模拟登录和完整媒体任务检查。长期生产路径和配置需随后续发布准备。此次正式应用发布版本仍为 `dfef7d5420e406e54625b3ef5788a324e8459913-1793650947008269408`。

## 已取得的真实证据

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 独立服务器代理访问 YouTube 连通性端点 | HTTP 204 | 操作记录 |
| Big Buck Bunny 元数据 | 11.37 秒，635 秒时长，1080p/720p/480p/MP3 | [首次报告](YOUTUBE_C_SERVER_PROBE.json) |
| 重启服务器代理进程后再次取元数据 | 9.793 秒，相同身份、时长和档位 | [重启后报告](YOUTUBE_C_AFTER_PROXY_RESTART.json) |
| Deno/EJS 离线执行 | Deno 2.9.8，EJS 0.8.0，版本与脚本校验通过 | 两份探测报告 |
| 用户服务、Linger、文件权限和二进制校验 | 全部通过，API 存活 | [最新审计](YOUTUBE_C_SERVER_AUDIT.json) |

样本为 `https://www.youtube.com/watch?v=aqz-KE-bpKQ`，元数据时长 635 秒。上述两次早期元数据探测运行在部署账号。最新完整业务验收运行在正式服务账号，使用一次性 PostgreSQL schema、独立 Celery 队列、存储及初始为空的 Redis DB 11/10，认证与额度保持开启。

| 最新完整业务检查 | 结果 |
| --- | --- |
| MP3 完整任务及认证文件取回 | 40.019 秒，15,232,868 字节，634.67102 秒，含音轨，完整有界解码通过 |
| 运行中取消 | 已传输 148,534 字节后取消，0.619 秒到达取消终态，进程组退出及任务临时目录清理通过 |
| 代理与隔离 API/Worker 重启后的 480p MP4 | 163.779 秒，38,895,635 字节，634.625 秒，含音视频轨，完整有界解码通过 |
| 用户归属和完成态 CAS | 两个阶段均通过其他用户任务/文件访问拒绝及完成后取消保护 |
| 测试 schema 和私有目录 | 两阶段 schema 与私有目录已清理，前轮遗留私有目录另经 root 清理并通过 SSH 确认 |

验收时间为 UTC 2026-11-04 13:03:14 至 13:07:31。两个阶段的全部应用源码 SHA256 均已在 C 交付时与本地源码核对。后续 D 对共享取源、选档和报价代码的修改另见 D 交付记录。早期元数据报告保留当时源码哈希。结果中的嵌套 `preflight` 保留能力预检原值，完整下载与重启证据由外层业务阶段字段记录。[完整媒体验收报告](YOUTUBE_C_HTTP_CELERY_PROBE.json)

临时候选节点 JSON 已删除，主私有代理配置和回滚备份保留。隔离 Redis 中仍可能存在测试序列键和队列绑定，未执行清库。

该样本返回的字幕语言为空，公开字幕取回尚待另选已确认有字幕的样本验证。服务器 FFmpeg 4.4.2 缺少现有切片命令要求的 `-fps_mode`，切片成片另有兼容验收前置。

## 配置、启动与复测

以下应用命令在 `backend/` 执行，使用管理员私有配置。API 和 Worker 应读取相同配置。正式账号路径就绪后再运行对应检查。

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

`USER:PASSWORD` 仅为占位，实际配置来自服务器私有文件。容器部署需要核对代理与应用的网络命名空间。

部署账号可检查已安装的用户代理服务。

```sh
systemctl --user is-active videohub-youtube-proxy.service
systemctl --user is-enabled videohub-youtube-proxy.service
loginctl show-user videohub-deploy -p Linger
```

API 与 Celery 启动命令如下，数据库与 Redis 沿用现有配置。

```sh
uv sync --locked
uv run python -m app.modules.video.preflight --platform youtube
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

可复用本阶段的隔离元数据探测。它只打包公开源码与锁文件，通过服务器精确匹配的依赖缓存构建一次性环境，并读取服务器私有客户端配置。显式风控失败触发 300 秒探测冷却。

```sh
uv run python scripts/youtube_stage_c_probe.py \
  --host videohub-deploy@129.28.26.221 \
  --identity /Users/edgar/.ssh/videohub_actions \
  --python /opt/videohub/.python/cpython-3.14.9-linux-x86_64-gnu/bin/python3.14 \
  --cache /opt/videohub/.cache/uv \
  --client-config /home/videohub-deploy/.config/videohub-youtube/client.json \
  --deno /home/videohub-deploy/.local/lib/videohub-youtube/deno \
  --url 'https://www.youtube.com/watch?v=aqz-KE-bpKQ' \
  --report /tmp/youtube-c-metadata.json
```

该命令仅用于部署账号的元数据检查。完整业务验证步骤见部署文档中的认证、报价、任务、受控文件和取消流程。[部署与 curl 验证](YOUTUBE_PRODUCTION.md)

## 测试与修改文件

完整回归为 **712 passed in 309.42s**，执行命令为 `uv run pytest`。YouTube 来源测试包含 36 项，加密 DNS 测试包含 21 项。模拟平台测试覆盖身份、格式、请求上限、出口隔离、运行时失败、错误分类、DNS 公网校验与超时。仓库既有真实 HTTP/Celery 测试随全量运行通过。

C 新增文件包括以下内容。

- `app/modules/video/parsers/{youtube,youtube_runtime,youtube_dns}.py`
- `tests/{test_youtube_source,test_youtube_dns}.py`
- `scripts/youtube_stage_c_probe.py`
- `deploy/youtube/{runtime-artifacts.json,youtube-proxy.yaml.example,videohub-youtube-proxy.service,videohub-youtube-proxy.user.service}`
- `YOUTUBE_C_{SERVER_AUDIT,SERVER_PROBE,AFTER_PROXY_RESTART,NETWORK_DIAGNOSTIC,AFTER_DNS_FIX,HTTP_CELERY_PROBE}.json` 与本交付记录

C 在 A/B 的工作区基础上继续修改了共享文件。

- `app/main.py` 与 `app/core/{config,security}.py`
- `app/modules/video/parsers/{dispatch,runtime,ytdlp,ytdlp_worker,cached,operations}.py`
- `app/modules/video/{preflight,service}.py`
- `app/modules/download/presets.py` 与 `app/infrastructure/process_runner.py`
- `.env.example`、`.env.docker.example`、`deploy/production.env.example` 配置模板
- `README.md`、`YOUTUBE_PRODUCTION.md`、`MULTIPLATFORM_SERVER_PLAN.md`

工作区中的 A/B 和更早修改仍混合存在。本轮只继续 C 的后端与交付工作，文件清单按里程碑职责归属列出。

## C 剩余验收与发布条件

1. 选择已确认有公开字幕的样本，验证字幕完整取回。
2. 按总体方案为每个平台预登记至少 30 个样本，覆盖至少 3 天，统计新鲜完整下载成功率、风控、缓存与冷却。当前 YouTube 仅完成一个样本的完整业务验证。
3. 在适当维护窗口验证整机重启后恢复。本次已验证代理进程及隔离 API/Worker 重启后的下载。
4. 准备正式账号长期可执行的 Deno 路径和私有客户端配置，按既有 PR/CI 与发布流程实施应用切换。
5. 切片成片仍需处理服务器 FFmpeg 4.4.2 的 `-fps_mode` 兼容问题，单独验证定位与成片流程。

## 验收中发现的问题与修正记录

初次准备受报告父目录权限影响退出。父目录已修正为 0711，私有测试子目录为 0700。后续正式账号诊断确认其能连接本地代理，但跨境 TLS 和 DNS 存在超时，旧 VLESS 节点也曾在 Worker 取源时失败。[网络分段记录](YOUTUBE_C_NETWORK_DIAGNOSTIC.json)

API 改为在生命周期内复用 HTTPS 连接，每次仍重新取得并校验 A/AAAA 答案。连接按管理员代理配置隔离并在关闭时释放。代理开启经当前节点发送的 AliDNS DoH，独立解析节点域名，监听 `127.0.0.1:17894`。应用最终采用连接最多 5 秒、读取最多 2 秒、查询组最多 8 秒，整体取源保持 26 秒。TLS 校验和私网拒绝规则保持开启。[DNS 修正后的历史元数据报告](YOUTUBE_C_AFTER_DNS_FIX.json)

节点验证中遇到访问限制和连通性失败，最终从用户现有订阅选取一条严格 TLS 的 AnyTLS 节点。该节点元数据探测为 8.097 秒，Worker 取源及格式处理通过，随后完成本次实际 HTTP/Celery 媒体验收。下载档位选择逻辑沿用现有实现。节点仍需随订阅维护。

最终后端全量回归为 **712 passed in 309.42s**，执行 `uv run pytest`。定向 YouTube 与健康检查为 **107 passed in 6.52s**。完整媒体报告中的应用源码哈希对应 C 本次回归版本，截至 C 交付只继续修改文档与审计记录。后续 D 的共享代码修改及测试独立记录。
