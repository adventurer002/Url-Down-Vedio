# 三平台改造：B 站专用取源（里程碑 B）

2026-11-03，分支 `codex/multiplatform-b`，在阶段 A 本地改动上继续实施。代码已完成，真实服务器单样本直连元数据解析通过；完整媒体下载及持续成功率未验收，生产未部署。未开始 C–E。

## 已实现的行为

platform 模式下，B 站普通 BV/av 视频进入专用 `BilibiliSourceBackend`。YouTube、抖音沿用阶段 A 的兼容内核，legacy 模式保持原行为；默认环境模板仍为 legacy。

主流程最多五次逻辑 API 请求：

1. 获取匿名 buvid 会话，仅放入 `api.bilibili.com` 的临时 CookieJar；支持合法 Cookie 字符及可缺省的 buvid4，不读取或接收用户登录 Cookie。
2. 获取公开 view 数据，确认 BV/av 身份、页面状态、付费/交互标志、所选分 P、CID 和该分 P 的时长。
3. 获取公开 WBI 图片密钥，按排序、字符过滤、时间戳和 MD5 算法签名；匿名 nav 的 -101 仅在同时含有效 WBI 数据时接受。
4. 获取 DASH 音轨和视频流描述，附加必要 Referer/UA，拒绝已知 DRM、试看及明显截断来源，不伪造可用清晰度。首选地址不符合安全策略时，检查同一响应中的有限个备用地址，选择符合现有域名、端口和公网规则的候选，不开放新端口或直接请求被拒绝的地址。
5. 获取字幕描述。元数据解析不下载媒体或字幕正文；只有字幕任务选定语言后，才有界获取一条字幕 JSON 并转换为现有 JSON3/TranscriptData 契约。

独立音轨与视频流继续走原下载、FFmpeg、本地存储、任务、报价、取消和终态处理流程；本阶段没有引入第二套下载器或 ASR，也没有修改 MP3 导出规格、切片渲染参数和定位服务。

身份兼容锁定版本 yt-dlp：多 P 默认第一 P 为 `_p1`；显式 `p=N` 保留 `_pN`（即使只有一个 P）；单 P 且无参数时保留裸 BV。av 别名以返回的 BV 建立原有平台身份，分 P 时长不使用全视频总时长。重复/非法 P、歧义页表及错配 CID 明确拒绝。

## 备用、预算与安全

- 只有已经确认公开身份和完整分 P 时长、随后遇到接口结构不兼容，才允许一次 yt-dlp 兼容备用；备用身份及完整时长必须与主流程一致。
- 412、429、明确风控、HTTP 错误、登录/付费/删除限制、试看、身份变化和不可信资源 URL 不触发备用。普通 403 不凭猜测累计平台熔断。
- 原生提取预算 18 秒，单次请求最长 5 秒，API 正文每次最多 2MiB。原生加备用共享 26 秒请求预算、最多 16 次实际 HTTP 请求（含重定向）；备用关闭 SDK 重试。原有父进程元数据总超时和进程组取消仍有效。
- 选中字幕正文最长 8 秒、5MiB、最多 30000 条，输出再次限制 5MiB。只转换真实字幕时间，不生成伪造 ASR 切点。
- 继续使用 NetworkGuard、公网 DNS 二次校验、TLS 与五跳重定向限制；只允许已列明的 B 站媒体/字幕域，匿名 Cookie 不放入流描述或 CDN 请求头。
- API/Worker 的 Redis 冷却沿用阶段 A。私有失败阶段（session/view/wbi/play/subtitle）只用于隔离进程和运维诊断，不加入公开响应、SSE 或业务任务结果。

## 真实服务器结果

最终报告：[BILIBILI_B_SERVER_PROBE.json](BILIBILI_B_SERVER_PROBE.json)。这是单样本的隔离元数据探测，不是生产下载任务或成功率基准。

| 项目 | 结果 |
| --- | --- |
| 时间 | 2026-11-03 13:18:20 UTC（北京时间 21:18:20） |
| 样本 | 用户提供的 BV1g2JX6nESP |
| 出口 | 服务器直连，显式关闭旧全局代理与 YouTube 代理 |
| 最终结果 | `OK`，耗时 0.826 秒；时长 1188 秒，9 个可用格式 |
| 已确认 | 匿名会话、视频元数据、WBI、完整时长检查、媒体格式描述与字幕描述查询均通过；该样本未返回公开字幕 |
| 未确认 | 音频/视频完整落地、真实字幕正文交付、持续成功率 |
| 生产变更 | 无；没有修改 backend.env、数据库、服务权限或重启服务 |

真实诊断发现并修正了匿名会话顺序、合法 Cookie 字符兼容及备用 CDN 选择。早期风控后遵守至少五分钟冷却；后续仅在有新代码/诊断依据时有限复测。播放接口实际返回成功且时长完整，地址选择修复后解析通过；不把早期单次风控归纳成“IP 一定被封”。报告仅保留阶段、数值状态码、时长及代码哈希，不回显原始响应、匿名标识或带签名的 CDN URL。

部署账号不能访问运行账号的私有虚拟环境。探测使用临时源码目录、服务器已有 Python 和公共 uv 缓存中的**锁定版本**依赖，未读取生产私有环境或更改权限；临时目录在结束后删除。探测脚本不下载新依赖，不运行付费模型，也不执行媒体任务。

保留 legacy 默认值及上传兜底。单样本元数据成功不等于完整取源验收，仍需通过 TaskService/Celery 完整下载、解码、取消和多样本验证，再决定生产启用。没有把本次诊断的直接元数据调用冒充生产任务链路验证。

## 启动及 curl / Swagger 验证

本地或之后已获准部署的测试环境中，API 与 Worker 使用同一份私有配置；`VIDEO_EGRESS_MODE=platform` 启用 B 站专用路径。无需新增配置项、数据库迁移或依赖。

```sh
# backend/，已有正确配置、PostgreSQL、Redis 和 FFmpeg
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另一终端同样在 `backend/`：

```sh
uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
uv run python -m app.modules.video.preflight --platform bilibili
```

带 URL 的预检会真实访问上游；仅在已有风控冷却结束且确有新诊断依据时执行，不为凑成功样本反复调用：

```sh
uv run python -m app.modules.video.preflight --platform bilibili \
  --url 'https://www.bilibili.com/video/BV1g2JX6nESP'
curl --fail --max-time 5 http://127.0.0.1:8000/api/v1/health/ready
curl --max-time 40 http://127.0.0.1:8000/api/v1/videos/parse \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"分享 https://www.bilibili.com/video/BV1g2JX6nESP?p=1 。"}'
```

`$TOKEN` 使用测试账号令牌。Swagger `/docs` 验证分享文本、BV/av、分 P、非法 P，并检查失败仍为统一 `error` 包装及 `request_id`。只有解析和预算检查都通过时，才通过原 `billing/quotes` → 确认 → `billing/tasks` 创建真实下载/转写任务，验证 SSE、取消和受控文件获取；标题成功不计作媒体下载成功。

隔离 SSH 元数据探测可复现，无需为重复证明一个成功结果反复访问上游：

```sh
uv run python -m scripts.bilibili_stage_b_probe \
  --host '<部署账号@服务器>' --identity '<私钥文件路径>' \
  --python '<服务器上可访问的现有 Python 3.14 路径>' \
  --cache '<服务器上可读取的 uv 缓存路径>' \
  --report BILIBILI_B_SERVER_PROBE.json
```

该脚本校验 SSH 主机指纹，仅读取精确匹配 uv.lock 的公共缓存依赖；它不具备生产发布能力。源码哈希记录在报告中，避免把旧代码探测结果用于验收新实现。

## 测试结果

最终版本 `uv run pytest`：**655 passed，304.33 秒**，包含备用 CDN 修复后的全部回归。B 站专项 **48 passed**。源码编译、`git diff --check`、文档本地链接与服务器报告源码哈希检查通过。

专项覆盖签名、分 P/av、会话字符与注入拒绝、公开范围、风控、响应大小、请求预算、单次备用、备用 URL 两种字段、字幕按需获取与子进程实际写入，以及元数据/下载入口的身份和风控一致性。原三平台/下载/字幕相关回归包含在最终全量中。本阶段不修改前端，不将 A 的前端成绩重复算作 B 的新验收。

```sh
# backend/
uv run pytest
uv run pytest tests/test_bilibili_source.py -q
```

## 本阶段文件清单

路径相对仓库根目录；A 的改动仍在同一工作区，以下仅列 B 新增或继续修改的文件：

| 文件 | 用途 |
| --- | --- |
| `backend/app/modules/video/parsers/bilibili.py` | 新增公开 B 站取源、WBI、匿名会话、格式与字幕转换 |
| `backend/app/modules/video/parsers/dispatch.py` | 原生平台分发、受控兼容备用、字幕准备契约 |
| `backend/app/modules/video/parsers/ytdlp_worker.py` | 实际请求计数、总预算、私有失败阶段 |
| `backend/app/modules/video/parsers/ytdlp.py` | B 站字幕能力及错误边界映射 |
| `backend/app/modules/transcript/subtitle_child.py` | 选中轨道后准备 B 站字幕，复用原写入流程 |
| `backend/app/infrastructure/process_runner.py` | 补齐受限来源响应/字幕错误映射 |
| `backend/scripts/bilibili_stage_b_probe.py` | 临时隔离服务器探测工具 |
| `backend/tests/test_bilibili_source.py` | B 站专项测试 |
| `backend/BILIBILI_B_SERVER_PROBE.json` | 最终真实成功探测记录，不含媒体 URL 或凭据 |
| `backend/README.md`、`MULTIPLATFORM_SERVER_PLAN.md`、`MULTIPLATFORM_B.md` | 阶段边界、结果及交付说明 |

阶段 B 的代码与离线验收可审查，服务器单样本直连元数据已通过，完整媒体取源仍未验收。此次没有合并或部署，不进入 C–E，不将其宣传为长期成功保证。
