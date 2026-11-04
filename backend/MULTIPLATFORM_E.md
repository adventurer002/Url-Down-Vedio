# E 阶段持续实测与发布准备

更新于 2026-11-05。

本阶段使用服务器托管的 72 小时隔离验收，测试经过认证 HTTP、报价、TaskService、Celery Solo 和受控文件接口。发布资格由实际结果、观察时间与安全检查共同决定。

## 样本与额度

预登记清单包含 B 站 30 个、YouTube 30 个和抖音 1 个样本。B 站候选来自用户链接及公开热门列表，YouTube 候选来自锁定 SDK 的公开测试记录及公开参考视频。清单在执行前冻结，候选当前可用性由实测确认。删除、风控、冷却及不可用结果继续保留在分母中。[预登记清单](acceptance-e-samples.json)

抖音当前保留用户提供的受限样本，样本量与取源能力均未达到发布门槛。后续补充抖音样本和可验证取源方案后，需要另行登记对应批次。

用户已授权向隔离测试账号发放测试会员权益。程序通过现有 `AdminService.grant_user` 记录审计，保持同一个测试身份及 PostgreSQL schema，重启继续使用已有权益和账务。身份通过隔离开发环境的现有登录口令与签名回调流程取得。ASR 关闭，LLM 使用本地 heuristic，实际业务任务只进行下载验收。

账号仍受会员单文件 512MiB、每日受控文件下行 5GiB、视频时长及服务端磁盘限制。测试样本另限制在 20 分钟内，下载总超时 600 秒。测试媒体保留期设为 1 小时，测试存储上限 5GiB。服务器验收服务限制 2GiB 内存、150% CPU、128 个任务和单 Solo Worker。代理流量沿用既有订阅。

## 执行方式与隔离

- 公共代码包按 SHA256 校验，目录由 root 持有，应用进程使用 `videohub` 账号。
- 一次性创建测试 schema、独立队列、初始为空的 Redis 数据库及私有存储。选择 Redis 时排除生产 API、Broker 和结果后端配置使用的数据库。
- 私有配置和状态目录为 0700，配置文件为 0600。脱敏报告使用单独的已知路径读取。
- `videohub-e-acceptance.service` 由 systemd 托管并启用开机启动，程序错误受限重启。计划在启动、24 小时和 48 小时执行三组样本，72 小时结束观察窗口。
- 下载前记录报价 ID 与任务 ID。程序中断后通过原报价恢复任务身份，取消未完成任务并记录中断结果，避免重复创建业务任务。
- 各平台沿用现有出口、SSRF、冷却、任务取消、Watchdog、容量与 CAS 规则。B 站和抖音直连，YouTube 使用服务器独立代理。

## 完整性与统计

媒体必须通过正常文件接口完整取回，校验大小、时长、音视频轨及有界完整解码。FFprobe 限时 15 秒，完整解码限时 180 秒，验证程序只允许本地文件协议。视频通过实际可用档位选择，MP3 导出与独立音轨取源分别记录。

隔离解释器启用只记录操作 ID、请求次数和选流模式的观测代码。元数据解释器以 `-m` 启动时也会记录 urllib HTTP 请求。报告不保存原始 URL 参数、请求头、Cookie 或代理配置。本批隔离元数据缓存 TTL 为 0，用于测量新鲜取源，缓存复用另由既有回归覆盖。成功结果缺少新鲜请求证据时记录 `TELEMETRY_MISSING`。

每个预登记样本分别记录元数据、音频导出和视频交付的最终结果。统计同时给出预登记数量、实际逻辑请求数量、新鲜 HTTP 请求数量、成功数、待执行数及错误分布。冷却拒绝计入逻辑失败，新鲜网络操作统计只包含实际发起过 HTTP 请求的操作。

元数据要求至少 29/30，音频和视频各至少 27/30，并要求三个实际观察日期及完整 72 小时窗口。服务账号、归属隔离、完成态保护、运行中取消、观测完整性、整机重启和确切 CI 发布产物均为独立检查项。缺项时发布资格保持关闭。

## 当前进度

服务器服务已启用并运行，实际账号为 `videohub`，UID 998。观察窗口从北京时间 **2026-11-05 00:42:05** 开始，计划在 **2026-11-08 00:42:05** 结束。最终资格以窗口结束后的报告为准。

首次安装后发现 systemd 的 0077 umask 让公开报告变为 0600。已为报告原子写入显式设置所需权限，增加严格 umask 的回归验证，并在样本完成边界受控重启同一服务。schema、测试身份、会员审计与已完成结果继续沿用，修正前后的操作程序哈希另外记录。

首批 21 个样本已完成，程序进入 `waiting_for_next_observation_day`。本地保存的是首批快照，后续状态以服务器报告为准。[首批完整报告](MULTIPLATFORM_E_SERVER_PROBE.json)

| 平台 | 元数据 | 音频导出 | 视频交付 |
| --- | --- | --- | --- |
| B 站 | 10/10 | 10/10 | 7/10 |
| YouTube | 9/10 | 8/10 | 7/10 |
| 抖音 | 0/1 | 0/1 | 0/1 |

B 站视频失败均为 `PRESET_UNAVAILABLE`。YouTube 包含一个 `UNSUPPORTED_URL` 元数据结果、一个音频访问受限结果及两个视频档位不可用结果。元数据失败后的两项媒体记录保留 `SOURCE_METADATA_UNAVAILABLE`。抖音继续记录服务器访问受限。

运行中取消耗时 0.616 秒，归属隔离、完成态保护与请求观测已通过。首批共 32 份媒体文件通过完整解码，文件取回总量 539,522,743 字节。YouTube 已知样本的 MP3 和 480p 文件 SHA256 与 C 阶段成功文件一致。本次还修正了安全检查失败被后续成功掩盖的判定，资格计算会从全部历史记录重新检查失败证据。两次操作程序修正均在样本完成后应用，沿用同一 schema、账号、会员审计与样本结果。[服务器审计](MULTIPLATFORM_E_SERVER_AUDIT.json)

多日样本、公开字幕、整机重启与确切 CI 发布产物仍待补齐。当前各平台发布资格均为关闭。

## 修改文件

- `scripts/acceptance_e.py`，持续验收、恢复与统计。
- `scripts/acceptance_e_trace/sitecustomize.py`，隔离解释器的请求和选流观测。
- `deploy/prepare_acceptance_e.py`，一次性 root 安装与 systemd 托管。
- `acceptance-e-samples.json`，执行前冻结的样本清单。
- `tests/test_stage_e.py`，统计、恢复、包校验和真实 API/Worker 登录与权益发放回归。
- `MULTIPLATFORM_E_SERVER_PROBE.json` 与 `MULTIPLATFORM_E_SERVER_AUDIT.json`，首批结果和服务器审计。
- 本交付记录及入口说明。

## 启动与检查

本地在 `backend/` 验证清单。

```sh
uv run python scripts/acceptance_e.py validate --manifest acceptance-e-samples.json
uv run pytest tests/test_stage_e.py -q
uv run pytest
```

本次安装已通过现有 1Panel root 终端直接完成，日常读取通过 SSH 执行。安装前核对了 `prepare_acceptance_e.py` 与公共源码包的 SHA256。报告位于 `/opt/videohub/acceptance/e-41a63bb12d41b6ea/reports/current.json`。

```sh
systemctl is-active videohub-e-acceptance.service
systemctl is-enabled videohub-e-acceptance.service
```

root 可通过同一个服务停止或恢复验收。

```sh
systemctl stop videohub-e-acceptance.service
systemctl start videohub-e-acceptance.service
```

隔离 API 与 Worker 始终由 `acceptance_e.py` 包装启动，由包装器设置测试 schema 和队列。隔离 API 使用随机回环端口。验收内部使用相同的认证入口完成以下顺序，人工复测沿用 C/D 文档中的 curl 与 Swagger 步骤。

```text
GET  /api/v1/health/ready
POST /api/v1/auth/wechat/sessions
POST /api/v1/auth/wechat/webhook
GET  /api/v1/auth/wechat/sessions/{scene_id}
POST /api/v1/videos/parse
POST /api/v1/billing/quotes
POST /api/v1/billing/tasks
GET  /api/v1/tasks/{task_id}
GET  /api/v1/files/{file_id}/download
POST /api/v1/tasks/{task_id}/cancel
```

人工验证仍需使用测试账号正常登录，保持认证与额度开启。隔离模拟回调只用于本次独立测试环境。[认证任务与文件核验步骤](YOUTUBE_PRODUCTION.md#curl-与-swagger-验证)

生产发布沿用已有 PR/CI、main 确切发布产物、维护窗口、配置迁移和回退流程。源码与配置分别保留版本记录，正式稳定版要求对应平台满足门槛。用户后来授权的加速试运行采用下文的独立短测要求。[发布流程](deploy/CD.md)


## 本地回归

阶段 E 专项 **26 passed in 5.15s**，包含真实 PostgreSQL、Redis、API 与 Celery 启动、稳定测试身份重复登录、受审计会员发放、Redis 生产库排除、严格 umask 下的报告权限、统计门槛及断点恢复。本轮完整 `uv run pytest` 为 **786 passed in 313.23s**，修正后的全部代码参与本次回归。

## 用户授权的加速试运行

2026-11-05 用户要求先修复档位问题，并在回归与完整服务器短测通过后更新试运行版本。试运行使用独立验收结论，72 小时稳定性资格继续按原门槛计算。原服务和原代码快照保持独立，原首批结果继续保留。

### 本轮修复

- B 站与 YouTube 在无法形成标准高度档位时提供 `video_source_mp4`，标签显示真实宽高，保留源比例。
- 音画分离来源分别下载视频与音频，使用本地 FFmpeg 合并。报价冻结视频和音频格式、编码、容器及协议，Worker 在取源后重新核对。
- 选择原画幅时优先选择像素面积不超过 1920×1080 的最大来源。全部来源超过该面积时选择最小来源。同面积优先 H.264。该规则由本轮实施选择，视频保持源尺寸。
- B 站格式 ID 使用质量 ID 与编码，消除返回列表重排引起的音轨身份误判。
- 已知来源大小、实时下载进度与落地文件实际大小均受音视频合计字节上限约束。非法体积提示不会绕过实际字节检查。

### 独立短测

`acceptance_e.py run-quick` 固定复测此前档位失败的 3 个 B 站视频、2 个 YouTube 视频，以及 1 个标准 YouTube 视频。每个样本完成元数据、MP3 和视频交付，共 18 项。验收经过认证 API、报价、TaskService、Celery、受控文件接口、音视频轨与时长检查、完整解码，并比对原画幅文件尺寸和冻结选择。

短测另外验证账号归属隔离、已完成任务的状态保护、传输中取消及请求观测。`trial_eligible` 要求全部 18 项与上述安全检查通过。多日发布门槛继续返回独立结果。

安装器使用 `--quick`，按源码包哈希创建单独的 systemd 服务、schema、Redis 数据库、队列、账号及存储。正式服务账号沿用现有受审计测试权益流程，使用已在服务器运行的 YouTube 专属出口。

首次短测发现 B 站音轨排列导致 `SOURCE_SELECTION_CHANGED`，同轮标准 YouTube 样本的一次音频请求返回 `SOURCE_ACCESS_RESTRICTED`。首次结果保留于服务器 `q-32c9aa2d4eaf2e9a` 报告目录。修复格式 ID 后，以新的不可变代码快照和独立状态重新验证，结果单独记录。

### 本轮文件与验证入口

本轮修改 `app/modules/video/service.py`、`app/modules/video/parsers/bilibili.py`、`app/modules/download/presets.py`、`app/modules/download/child.py`、`scripts/acceptance_e.py`、`deploy/prepare_acceptance_e.py`，补充 `tests/test_native_video.py` 以及下载、B 站和 E 验收测试。

本地常规启动命令如下，正式服务器继续由既有 systemd 服务管理。

```sh
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=INFO
uv run pytest
```

在 `/docs` 登录后依次调用解析、报价、确认任务、查询任务、下载文件接口。B 站样本 `https://www.bilibili.com/video/BV1RA4w6AE74` 应返回 `video_source_mp4` 和真实尺寸标签。使用 `BearerAuth` 及现有登录令牌完成验证，令牌保留在本机或服务器私有环境。

```sh
curl --fail --max-time 35 -H "Authorization: Bearer $VIDEOHUB_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.bilibili.com/video/BV1RA4w6AE74"}' \
  https://vedio.vip/api/v1/videos/parse
```

随后 `POST /api/v1/billing/quotes` 使用 `action=download` 与 `preset_id=video_source_mp4`，再将返回的 `quote_id` 传给 `POST /api/v1/billing/tasks`。通过 `GET /api/v1/tasks/{task_id}` 等待完成，使用返回的文件下载入口取回完整 MP4。检查原画幅宽高、音视频轨和完整解码结果。

第二轮短测已通过，18/18 项成功，12 份完整媒体文件通过解码，归属隔离、完成态保护、请求观测及传输中取消通过。取消耗时 0.616 秒。5 个原档位失败视频均成功交付，实际画幅分别为 854×356、480×652、854×450、1080×1920、1080×1482，标准对照为 854×480。服务器源码的 158 项应用文件哈希与本地待发布代码逐项一致。[独立短测报告](MULTIPLATFORM_QUICK_SERVER_PROBE.json)

短测包 SHA256 为 `1889ba92786ba82dbab269d1ddfc1937444a555c59643b009c7bd0e8b37ff0b6`。正式运行时准备了校验通过的独立 Deno 路径 `/opt/videohub/runtime/youtube/deno`，代理配置从服务器既有私有文件注入，保留生产配置文件原所有者和权限。发布仍通过 main CI 的确切产物、数据库备份、Worker 正常收尾、原子版本切换及健康检查。

现有前端改动的类型检查、346 项单元测试及构建全部通过。本轮最终后端全量回归为 **809 passed in 317.26s**。试运行发布状态以交付回复为准。
