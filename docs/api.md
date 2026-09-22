# API 设计

REST + SSE，前缀 `/api/v1`。本文档定义端点、出入参与错误约定；字段级 schema 以代码中的 Pydantic 模型为准，但端点增删、语义变更必须先改本文档。

## 1. 通用约定

**鉴权**：`Authorization: Bearer <access_token>`。标注「登录」的端点必须携带有效 token；标注「可选」的端点未登录时按游客处理。

**错误格式**：所有 4xx/5xx 返回统一结构：

```json
{
  "error": {
    "code": "quota_exceeded",
    "message": "今日免费下载次数已用完",
    "details": {}
  }
}
```

`code` 是机器可读的稳定标识，前端只允许依赖 `code` 分支，不允许匹配 `message` 文本。

**分页**：`?page=1&page_size=20`（page_size 上限 100），返回 `{items, total, page, page_size}`。

**限流**：超限返回 429，带 `Retry-After` 响应头与 `error.code=rate_limited`。

**幂等**：创建类端点（downloads、orders）接受 `Idempotency-Key` 请求头，同用户同键 24h 内返回首个结果，不重复创建。

## 2. 错误码

| code | HTTP | 含义 |
|------|------|------|
| invalid_url | 400 | URL 格式非法 |
| url_not_allowed | 400 | 协议或目标地址不被允许（SSRF 拦截） |
| unsupported_platform | 400 | 平台暂不支持 |
| validation_error | 422 | 请求体校验失败 |
| unauthorized | 401 | 未登录或 token 失效 |
| permission_denied | 403 | 当前套餐无此权益 |
| quota_exceeded | 429 | 超出配额（与 rate_limited 区分：一个是套餐用量，一个是请求频率） |
| rate_limited | 429 | 请求频率超限 |
| not_found | 404 | 资源不存在或不属于当前用户 |
| task_not_cancellable | 409 | 任务已进入终态或 uploading，不可取消 |
| parse_failed | 502 | 平台解析失败（详情在 details.error_code） |
| download_failed | 502 | 下载/转码失败 |
| internal_error | 500 | 未预期错误 |

task 记录里的 `error_code` 复用上表 code 集合，另补充 worker 内部码：`platform_blocked`、`format_unavailable`、`ffmpeg_error`、`asr_error`、`llm_error`、`storage_error`、`timeout`。

## 3. 权限矩阵

| 能力 | 游客 | 免费用户 | 会员 |
|------|------|----------|------|
| 解析视频 | ✓（限频更严） | ✓ | ✓ |
| 下载视频 | — | ✓（每日配额 + 时长上限） | ✓（配额更高） |
| 历史记录 | — | ✓ | ✓ |
| 音频提取 / 转写 / 总结 / 思维导图 | — | — | ✓ |
| Ask Video | — | — | ✓（上线后） |

矩阵是 `plans.features` 配置的默认值示例，运行时以 DB 配置为准，`PermissionService` 输出拒绝时带 `permission_denied` 或 `quota_exceeded`。

## 4. Auth 与用户

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| POST | /auth/register | `{email, password, nickname?}` → 创建用户并返回 token 对 | 无（IP 限频） |
| POST | /auth/login | `{email, password}` → `{access_token, refresh_token, user}` | 无（IP 限频） |
| POST | /auth/refresh | `{refresh_token}` → 新 access_token | 无 |
| GET | /users/me | 当前用户信息 + 当前订阅 + 今日用量 | 登录 |

token 约定：access 2h，refresh 14d；refresh 一次性轮换，旧 refresh 立即失效。

## 5. 视频解析与下载

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| POST | /videos/parse | `{url}` → `{video_id}`；video 初始 status=parsing | 可选 |
| GET | /videos/{id} | 视频详情：title、uploader、thumbnail、duration、formats、status、error_message | 可选（匿名资源仅创建者凭返回的 id 可见） |
| GET | /videos | 当前用户的解析历史（分页） | 登录 |
| POST | /downloads | `{video_id, format_id}` → `{task_id}`；前置：权限 + 配额 + 视频 status=ready | 登录 |
| GET | /downloads | 当前用户任务列表（分页，可按 status 过滤） | 登录 |
| GET | /downloads/{id} | 任务详情：status、progress、error、产物 media_file | 登录 |
| POST | /downloads/{id}/cancel | 取消任务；仅 uploading 之前允许，否则 `task_not_cancellable` | 登录 |
| GET | /downloads/{id}/events | SSE 进度流，见 §8 | 登录 |
| GET | /downloads/{id}/file | 302 跳转 OSS 签名 URL；仅任务 completed 且文件未过期 | 登录 |

`POST /videos/parse` 返回 202 而不是 200：解析是异步过程，客户端随后通过 `GET /videos/{id}` 轮询或等待 status 变化。解析完成（ready/failed）才算这次请求有了结果。

## 6. AI 功能

全部仅会员可用，全部异步：POST 创建一条 `processing_tasks` 记录并返回 `{task_id}`，前端轮询 `GET /tasks/{task_id}` 获取状态，产物就绪后经对应的 GET 端点读取。同一视频同一功能重复触发时返回进行中的 task 或已有产物，不重复执行。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /videos/{id}/audio | 提取音频 → 产物为 media_files(kind=audio) |
| POST | /videos/{id}/transcribe | 触发 ASR → 产物为 transcript |
| GET | /videos/{id}/transcript | `{language, text, segments}` |
| POST | /videos/{id}/summarize | 触发总结；需要 transcript 已存在 |
| GET | /videos/{id}/summary | `{summary, key_points, chapters, keywords}` |
| POST | /videos/{id}/mindmap | 触发思维导图生成；需要 summary 已存在 |
| GET | /videos/{id}/mindmap | `{markdown}`，前端 Markmap 渲染 |
| GET | /tasks/{task_id} | processing_tasks 状态查询：`{task_type, status, error, output_id}` |

依赖顺序由服务端校验：transcribe 前可自动触发 audio_extract，但 summarize 不会自动触发 transcribe——缺前置产物时返回 409 + `prerequisite_missing`，由前端引导用户逐步操作。

## 7. 套餐与支付

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| GET | /plans | 在售套餐列表 | 无 |
| POST | /orders | `{plan_code}` → `{order_no, amount, pay_params}`；金额从 plan 快照 | 登录 |
| GET | /orders | 当前用户订单列表 | 登录 |
| GET | /orders/{order_no} | 订单详情（前端轮询支付状态用） | 登录 |
| POST | /webhooks/payments/{provider} | 支付结果通知，验签 + 幂等 | 渠道签名校验 |

webhook 处理规则：验签失败直接 400；重复通知（同 provider_trade_no）返回 200 但不产生副作用；支付成功后开通/续期 subscription 并置 order paid，整个操作在一个事务内。前端展示的任何「支付成功」都不作为开通依据。

## 8. SSE 事件格式

`GET /downloads/{id}/events`，`Content-Type: text/event-stream`：

```text
event: progress
data: {"task_id": "...", "status": "downloading", "progress": 42.5}

event: progress
data: {"task_id": "...", "status": "uploading", "progress": 100.0}

event: end
data: {"task_id": "...", "status": "completed", "media_file_id": "..."}
```

连接建立后立即回放一次当前状态（来自 DB），之后转发 Redis pub/sub 的增量事件。任务进入终态（completed/failed/cancelled）时发 `end` 并关闭连接。前端断线重连直接重新建立连接即可，不需要补拉中间事件。

## 9. 系统

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /healthz | 存活探针，固定 200 |
| GET | /readyz | 就绪探针，检查 DB 与 Redis 连通 |
| GET | /usage | 当前用户当月用量与配额（AI 页、个人中心展示用） |

管理后台 API（用户/订单/任务/成本看板）挂在 `/admin/` 下、要求 `is_admin`，Phase 9 设计时单独成节补充到本文档。
