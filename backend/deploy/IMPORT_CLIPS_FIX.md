# 服务器解析与精彩切片发布修复（2026-11-03）

## 原因及本轮修改

服务器直连所测 B 站视频页面收到 HTTP 412，平台明确拒绝该次访问；本机可以解析相同链接。
生产 CI 只传入 API 地址，没有设置 VITE_CLIPS_ENABLED，服务端实际 CLIPS_ENABLED=false。

- CI 生产构建显式设置 VITE_CLIPS_ENABLED=true。入口位于导入内容后的工作台“精彩切片”标签。
- 412 和 B 站风控提示映射为 SOURCE_ACCESS_RESTRICTED，继续隐藏第三方诊断和凭据。
- 元数据解析遇到访问限制/限流后，对相同 hostname 默认冷却 30 秒（每个 API 进程），排队中的请求也检查冷却。成功缓存仍可读，DNS 安全校验始终先执行。
- VIDEO_PARSE_FAILURE_COOLDOWN_SECONDS 允许 0–300 秒；普通解析失败、超时和资源不可用不进入风控冷却。冷却记录最多保留 64 个，不增加无限重试。
- 首页风控、限流或出口故障提示提供“改用上传文件”，切换回来保留原链接。
- B 站等非 YouTube 链接的诊断不再因 Deno 未启用而跳过实际解析。

## 配置与发布

不要用模板覆盖生产私有配置。API 和 Worker 共用 /etc/videohub/backend.env，在保留权限和备份后核对：

```dotenv
CLIPS_ENABLED=true
VIDEO_PARSE_FAILURE_COOLDOWN_SECONDS=30
# 必须填写服务器能够实际连接的出口。空值继续直连。
YTDLP_PROXY=
```

原有 ALLOWANCES_ENABLED=true、ASR/LLM 模型和密钥继续使用现有配置。
千问 qwen-audio-3.0-asr-flash 当前适配器返回分块文本，足以获取文稿及分析候选，不提供逐句精确定位。
视频生成还需单独配置：

```dotenv
CLIPS_TIMING_API_BASE_URL=
CLIPS_TIMING_API_KEY=
CLIPS_TIMING_MODEL=
```

此定位接口要求 /audio/transcriptions、verbose_json、segments 真实时间戳。不得把千问聊天式 ASR 地址直接填入冒充已兼容的定位服务。
未配置时保留候选分析，视频生成返回 CLIP_TIMING_NOT_CONFIGURED，不伪造精确切点。

按现有 CI → 成功的 main 发布产物 → Deploy production 流程上线，迁移和服务重启由发布程序执行。
仅更新配置时，管理员执行 `systemctl restart videohub-api videohub-worker`；Worker 继续正常收尾，不强杀活动任务。
本轮没有新增数据库迁移。

## Mac Clash 临时出口验证

Mac 的 127.0.0.1:7890 与服务器的 127.0.0.1 不同。下列命令在 Mac 执行，服务器监听仅绑定回环：

```sh
ssh -N -i ~/.ssh/videohub_actions \
  -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o ConnectTimeout=10 -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
  -R 127.0.0.1:17890:127.0.0.1:7890 \
  videohub-deploy@129.28.26.221
```

先验证该通道，再将服务器 YTDLP_PROXY 配为 http://127.0.0.1:17890。
前台模式 Ctrl+C 关闭。Mac 休眠、Clash 停止或 SSH 断线都会使该出口不可用，不能把临时隧道当作已建成的独立生产代理。
停止前先恢复服务器先前的出口配置并重启服务。长期运行应改为不依赖 Mac 在线的服务器出口；不开放公网代理端口，不上传 Cookie。

## 启动与验证

本机三个终端分别运行（已有 PostgreSQL/Redis 和私有环境配置）：

```sh
cd backend
CLIPS_ENABLED=true ALLOWANCES_ENABLED=true uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```sh
cd backend
CLIPS_ENABLED=true ALLOWANCES_ENABLED=true uv run celery -A app.infrastructure.queue.celery_app:celery_app worker --pool=solo -c 1 --loglevel=info
```

```sh
cd frontend
VITE_CLIPS_ENABLED=true pnpm dev
```

健康、元数据与 Swagger 验证（TOKEN 是已有账号的登录凭证，不是资源下载凭证）：

```sh
curl --fail --max-time 5 https://vedio.vip/api/v1/health/ready
curl --max-time 40 https://vedio.vip/api/v1/videos/parse \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.bilibili.com/video/BV1g2JX6nESP?t=3.6"}'
```

本机 `/docs` 验证解析、切片报价、billing/tasks、任务进度和取消；生产文档如关闭，则使用网站登录后的同名流程。
工作台检查：解析成功 → 可用档位 → 下载报价与确认 → 受控文件取回；“精彩切片”→ 分析报价与确认 → 候选。
具备真实定位配置后，才能继续验证生成报价、MP4 下载和中途取消。解析或测试替身通过不等于真实成片模型验收通过。

复验命令：

```sh
cd backend
uv run pytest
uv run python -m app.modules.video.preflight --url 'https://www.bilibili.com/video/BV1g2JX6nESP?t=3.6'
cd ../frontend
pnpm type-check
VITE_CLIPS_ENABLED=true pnpm test:unit
VITE_API_BASE_URL=/api/v1 VITE_CLIPS_ENABLED=true pnpm build
```

本轮本地结果：后端 557 passed，前端 339 passed，类型检查及生产构建通过。

修改文件：.github/workflows/ci.yml；.env.example；.env.docker.example；
backend/app/core/config.py；backend/app/modules/video/parsers/{cached,ytdlp}.py；
backend/app/modules/video/preflight.py；backend/deploy/production.env.example；本文；
backend/tests/{test_performance,test_youtube_server_config}.py；
frontend/src/views/HomeView.tsx；frontend/src/utils/errorPresentation.ts；frontend/src/tests/reactApp.test.tsx。
