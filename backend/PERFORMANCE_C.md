# 性能方案 C 包 · 文件分发与下载提速

适用机型为轻量云 4 核、8GB、10Mbps。生产启用等待用户确认。

## 实现

- 本地成品登记后即可交付。后台 Worker 将公开下载成品复制到私有 S3 兼容桶，并在上传成功、对象 HEAD 的长度一致后登记镜像。上传地址和读取地址独立配置。单机同一时刻只上传一个镜像，每个文件最多尝试三次，补发由现有 reconcile 定时任务处理。
- 启用分发后，授权下载返回 307 到独立 HTTPS 网关。网关用一次性、60 秒内有效且绑定 Range 的凭证领取流量预留，再从私有桶流式读出，按 ASGI 已发送字节结算。领取由数据库条件更新保护，重复回调只结算一次。过期未领取的预留释放，已领取却丢失回报的预留按 1 小时规则对账。
- 网关负责发送文件，应部署在拥有独立出口的服务上。主机只处理签发与计量请求。兼容单段 Range 和断点续传，新范围重新访问原下载地址签发。If-Range 继续走本地路径以保留原有条件请求语义。已有本地成品继续支持抽音、剪辑和回退。
- 来源缓存初版仅处理原生 B 站匿名 DASH。媒体 URL 必须属于已校验的 CDN，有可确认的 deadline，且请求头无需用户会话。Redis 缓存最长 120 秒并保留地址到期余量。命中后沿用原网络校验，确认地址失效时在同一总时限内重新取源一次。其他平台继续由各自适配器处理。
- B 站直连双流可各用独立 yt-dlp 实例，单次任务最多两路传输，共用体积和超时上限。满足 fast-start 布局且由 ffprobe 确认只有兼容 H.264 与 AAC 轨道的 MP4 可直接交付，其余沿用 FFmpeg。
- 独立任务目录的 500ms 监控继续检查取消、时限、体积和实际磁盘余量。已预留空间的下载每 5 秒复核全局容量，准入和提交仍做完整检查。

## COS 直下

用户确认直下模式按签发时的整文件大小扣当日流量额度。现有 `GET /api/v1/files/{file_id}/download` 在鉴权后签发最长 120 秒的 COS GET 链接，并以 307 跳转。签名不会晚于成品的有效期。额度扣减与签发流水在同一数据库事务提交，账本事件为 `object_link_issued`，不产生需要回调结算的传输预留。

完整下载和单段 Range 都可复用同一个有效链接。持有链接者在有效期内可以再次访问，短链过期后需回到应用申请新链接，每次签发都计整文件额度。COS 实际发送字节与应用签发额度是两个口径。镜像未登记、签名配置不可用或请求显式指定 `?delivery=local` 时走本地交付。

在隔离环境的配置文件填写以下项目，再为测试进程提供对应命名空间的 COS 凭据。API 用 GetObject 签发下载，镜像流程用 PutObject 和 HeadObject 上传及校验。生产命名空间需要单独授权。

```dotenv
OBJECT_MIRROR_ENABLED=true
OBJECT_DELIVERY_ENABLED=true
OBJECT_DELIVERY_MODE=direct
OBJECT_BUCKET=vedio-1462213558
OBJECT_REGION=ap-chengdu
OBJECT_NAMESPACE=cos-chengdu-acceptance
OBJECT_UPLOAD_ENDPOINT=https://cos.ap-chengdu.myqcloud.com
OBJECT_READ_ENDPOINT=https://cos.ap-chengdu.myqcloud.com
```

使用本章的 API、Worker 启动命令及迁移步骤。测试账号生成下载成品并等待镜像完成，再通过 Swagger 调用文件下载入口。验证首跳 307、COS 返回状态及字节数，并查看 `GET /api/v1/billing/allowances` 和 `GET /api/v1/billing/ledger` 确认整文件扣额。可通过 curl 检查单段范围。

```sh
curl -fsS --max-time 30 -L --range 0-1023 -o /dev/null \
  -w '%{http_code} %{size_download}\n' \
  -H "Authorization: Bearer $VIDEOHUB_TEST_TOKEN" \
  "$VIDEOHUB_API_URL/api/v1/files/$VIDEOHUB_TEST_FILE_ID/download"
```

直下模式使用 COS 默认域名。现有网关模式和一次性结算流程仍可通过 `OBJECT_DELIVERY_MODE=gateway` 使用。生产部署需另行确认。

2026-11-22 使用成都云机的独立 PostgreSQL 迁移库和测试 COS 前缀完成端到端验收。镜像前的 Range 请求从本地返回 206，镜像后已鉴权的 Range 请求返回 307 并一次扣除整文件的 8,388,608 字节。非所有者返回 403。外部客户端使用同一个签名链接完整下载和读取 1 MiB 范围，分别返回 200 和 206，数据及中文下载文件名均通过校验。测试对象删除返回 204。

本次 `uv run --offline pytest` 共 876 项通过，对象交付专项复核 8 项通过。

## 启动与接口验证

在隔离开发环境中启动 PostgreSQL、Redis，进入 backend 目录。

```sh
uv sync --locked
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Worker 按 [B 包交付说明](PERFORMANCE_B.md) 启动。检查对象路径时，给 API 和上传 Worker 配置私有桶和上传端点。独立网关配置同一命名空间、桶、地域、读取端点、独立的对象读取凭据、回调 API HTTPS 地址和共同的分发密钥。

腾讯云 COS 使用地域端点，例如成都填 `https://cos.ap-chengdu.myqcloud.com`。在 `OBJECT_BUCKET` 填完整桶名（含 APPID 后缀），在 `OBJECT_REGION` 填 `ap-chengdu`。适配器将桶名放入签名请求的主机名。通过同地域云机解析桶的默认域名可以确认内网连通性，真实上传、HEAD 和 Range 读取需要使用隔离验收凭据。[腾讯云 S3 兼容配置](https://www.tencentcloud.com/zh/document/product/436/34688)

```sh
uv run uvicorn app.modules.media.gateway:app --host 127.0.0.1 --port 8001 --no-access-log
```

网关通过反代提供 HTTPS。反代的日志应记录路径或请求 ID，并隐藏查询参数及回调密钥。初始部署保持两个对象开关关闭。只有可读写、Range 及流量对账验证通过后，才启用 OBJECT_DELIVERY_ENABLED。三个下载提速开关可分别启用和回退。

```sh
curl -fsS --max-time 5 http://127.0.0.1:8000/api/v1/health/ready
curl -fsS --max-time 5 http://127.0.0.1:8000/openapi.json -o /dev/null
curl -fsS --max-time 5 http://127.0.0.1:8000/docs -o /dev/null
```

打开 http://127.0.0.1:8000/docs，用测试账号提交下载并等待任务完成。镜像登记后，用同一账号验证已授权文件的 Range 和本地回退。

```sh
curl -fsS --max-time 15 -D - -o /dev/null --range 0-1023 -L \
  -H "Authorization: Bearer $VIDEOHUB_TEST_TOKEN" \
  "$VIDEOHUB_API_URL/api/v1/files/$VIDEOHUB_TEST_FILE_ID/download"

curl -fsS --max-time 15 -o /dev/null --range 0-1023 \
  -H "Authorization: Bearer $VIDEOHUB_TEST_TOKEN" \
  "$VIDEOHUB_API_URL/api/v1/files/$VIDEOHUB_TEST_FILE_ID/download?delivery=local"
```

第一条应观察 307 后的 206 与对应的 Content-Range。再次向网关请求同一凭证应返回 409。不参与调试的人员不得接触包含凭证的 Location。第二条验证授权和流量额度仍由原接口执行。

## 部署审批前的验证范围

- 本地已使用 S3 签名与受控 HTTP 对象端点验证 PUT、HEAD 校验、带范围读取、失败回退和计量回调。
- 2026-11-21 在成都轻量云机验证了成都 COS 测试前缀的真实上传。桶域名解析到内网地址 `169.254.0.47`，8 MiB 对象的 PUT 与 HEAD 合计 380 ms，折合 176.83 Mbps。完整下载 SHA-256 一致，1 MiB Range 返回 206 并逐字节匹配，测试对象删除返回 204。该结果来自一次短样本，独立网关公网吞吐与持续负载待测。
- 2026-11-21 在本机用隔离 HTTPS API、实际网关服务和临时 PostgreSQL schema 验证真实 COS 公网回源。云机上传测试对象，网关通过本机公网连接读取短效签名 URL。8 MiB 完整下载耗时 555 ms，1 MiB Range 返回 206，两者哈希一致并按 8,388,608 字节和 1,048,576 字节独立结算。非所有者和无密钥回调返回 403，范围不符返回 416，凭证重放返回 409，测试对象删除返回 204。公网网关的用户入口、吞吐和生产回调仍需在拟部署环境验收。
- 桶保持私有。为版本化对象前缀设置适合业务文件 TTL 的生命周期，同时清理未登记的上传对象。24 小时文件 TTL 可用更长的桶生命周期留出恢复余量，过期后 API 停止签发。
- 需要应用数据库迁移 0017。对象分发、镜像上传及三个下载提速开关均可独立关闭。回退时先禁用新签发并等待已签发链接过期。网关模式还需处理现有流量预留。数据迁移保留为向后兼容字段，未结算对象传输会阻止 0017 降级。
- 网关发布、生产迁移和真实长视频持续负载验收尚未执行。[D 包隔离试跑](PERFORMANCE_D.md)

## 修改文件

- 数据库为 `alembic/versions/0017_object_delivery.py`。
- 云对象与计量为 `app/infrastructure/storage/objects.py`、`app/modules/media/{models,repository,service,delivery,gateway,mirror}.py`、`app/modules/billing/{models,repository,manage,transfer}.py`。
- 下载与磁盘监控为 `app/modules/download/{adapter,child,fast_mp4,worker,router}.py`、`app/infrastructure/{process_runner.py,storage/base.py,storage/local.py}`、`app/modules/video/parsers/{cached,source_cache}.py`。
- 配置与装配为 `app/core/{config,logging}.py`、`app/api/v1/router.py`、`app/infrastructure/queue/celery_app.py`、`backend` 下两份 dotenv 示例和 `deploy/production.env.example`、`pyproject.toml`、`uv.lock`。
- 测试及交付为 `tests/{test_object_delivery,test_download_performance_c,test_clips_r3_postgres}.py`、`reviews/performance-c-tests.xml`、`README.md`、`DOWNLOAD_PERFORMANCE_PLAN.md`、本文件。

## 测试结果

最终运行 `uv run --offline pytest`，**872 项全部通过**，耗时 333.08 秒。专项验证覆盖真实 PostgreSQL 迁移、权限隔离、对象上传和长度校验、一次性 Range 凭证、下载中断按已发送字节结算、失效回退、B 站缓存与双流合并大小限制、原样交付兼容 MP4 和重封装不兼容布局。[测试报告](reviews/performance-c-tests.xml)

临时 API 的存活、就绪、Swagger 和 OpenAPI 均返回 HTTP 200。临时网关使用测试密钥并对无效凭证返回 HTTP 403。依赖锁与 `git diff --check` 检查通过。
