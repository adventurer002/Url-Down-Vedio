# VideoHub AI：新对话交接与管理系统开发起点

整理日期：2026-10-25。本文是交接上下文，不代替代码检查。新对话先读AGENTS.md并核实Git/运行状态；不需要重装已有环境。本文不包含任何密码、API Key、Token或私钥。

## 1. 用户接下来要做什么

用户边学习边做项目，已经走通GitHub Actions部署，现在希望在现有项目上开发管理系统。优先做适合单人、小规模项目的第一版，分步骤解释，每步可验证；不要重新搭一套项目或加入不必要服务。

管理系统的具体功能范围尚未正式确认。先检查现状，提出精简方案和阶段划分，再按确认范围实现。不要把本文的建议范围当成已实现或已确认需求。

## 2. 仓库、规则与技术栈

- 本地项目：`/Users/edgar/Desktop/项目/videohub_ai`。
- GitHub：`https://github.com/adventurer002/Url-Down-Vedio`；远程origin为SSH。
- 本地当前main；最新本地提交和已部署提交：`5fa92bc953b39b47bde882add59653b55c615b91`，生产部署PR #3已合并。
- 历史阶段1–6限制仅backend；项目已到阶段9及后续授权功能，frontend已经存在且获得开发授权。继续遵守AGENTS，不自行跨新的实施阶段。
- 后端FastAPI、SQLAlchemy、Alembic、PostgreSQL16、Redis7、Celery；Python >=3.14，后端在backend/使用uv和uv.lock。
- 前端React19、TypeScript、Vite7、pnpm10.32.1；以package.json/锁文件为准。不是Vue。
- 模块通过Service/Repository契约交互，不能从管理模块任意修改其他模块ORM绕过账务/任务规则。
- 外部能力经过typing.Protocol；所有外部I/O明确超时、有限重试。耗时任务由TaskService与Celery执行。
- PostgreSQL是账务事实来源；Redis缓存、协调、SSE事件。复用现有服务。
- Solo Worker `--pool=solo -c 1`；取消遵守独立进程组、500ms Watchdog、SIGTERM→SIGKILL、确认退出后清理、数据库CAS。
- `/api/v1`统一路由，成功`{"data":...}`，错误`{"error":{"code","message","request_id"}}`；遵守现有缓存键、SSRF防护、SSE先订阅后快照/seq去重、live/ready规范。
- 存储预算必须包含合并/转码临时峰值。阶段结束运行uv run pytest，并报告修改文件、启动与验收、测试结果。

## 3. 已有业务能力与当前额度规则

已实现公开免登录资源解析/下载、音频提取、字幕或ASR转写、总结、思维导图、微信登录、任务历史、取消、SSE，以及订阅页、额度报价确认、视频问答、个人收藏/标签/搜索。

最初讨论过积分钱包，后来改为“转写分钟 + AI次数”的简单额度制。不要重新按最初的200/2000积分方案实现。

- 首次关注并绑定登录体验：30分钟转写、5次AI，30天有效，防重复领取。
- 月会员：¥19.90，每账期300分钟转写、100次AI，手动续费，不结转。
- 加购：¥9.90/120分钟转写，或¥9.90/100次AI；发放起30天有效。
- 免费下载每天成功2次，北京时间重置；免费账号任务并发额度1、会员3，但单Solo Worker实际串行。
- 免费/会员视频上限20/60分钟、单文件256/512MiB、每日下行1/5GiB；媒体24小时保留，重复HTTP取回/Range按实际发送流量计量。
- ASR按整段真实音频时长向上取整一次，以整数秒核销；字幕、转写缓存不扣ASR额度。
- 总结+导图整体成功计1次AI；单独功能独立结算。公共AI缓存首次获取计1次，自己的已获取相同结果再看免费。
- 问答使用本人已授权文稿，新回答计1次AI、不扣转写；相同用户/文稿/问题/模型版本的成功结果免费复用，不跨用户共享私有问答。
- 排队不冻结，执行前原子占用确认上限；成功交付后结算，失败或有效取消释放；平台重试不重复收费；已完成不可被迟到取消覆盖。
- 产品规则和限制当前主要在billing/policy.py中，不是假定已有可在线编辑价格的后台。
- 没有真实支付渠道、支付回调或线上购买能力。仅有带actor和幂等key的人工grant CLI；人工发放不能当支付到账证据，不能据此统计真实收入。
- 用户要求正常交付不支持退款；现有规则文档表述为正常交付不支持无理由退款，支付异常/依法应退另行处理。任务失败释放额度是另一机制，不等于现金退款。
- 用户称语言模型为deepseek-flash，ASR为中国内地千问3 ASR Flash；配置指导使用qwen3-asr-flash。真实服务器模型ID/凭据可用性及成本尚未在此次部署验收，不编造模型价格或测试成功。

## 4. 已有管理能力（实际核对）

- `frontend/src/views/AdminView.tsx`，路由`/admin`：只有角色admin可见的API/PG/Redis健康展示，每15秒检查；不是完整管理系统。
- `frontend/src/views/SettingsView.tsx`，`/settings`：有清理过期文件按钮，当前普通用户仍能看见按钮，但后端会拒绝非admin；这是可改进的权限展示不一致。
- `backend/app/modules/auth/models.py`：User已有role和status；`auth/dependencies.py`已有required_user/required_admin。
- `backend/app/modules/system/router.py`：全局POST `/api/v1/system/cleanup`已要求required_admin。此前的未鉴权问题已修复。
- `backend/app/modules/auth/router.py`：此前验签日志记录Token的问题已修复，不要恢复敏感日志。
- `backend/app/modules/billing/manage.py`：人工grant和reconcile CLI；`billing/router.py`现有接口主要是用户自己的catalog/allowances/ledger/quotes/tasks/resume。
- 尚未实现完整管理员用户列表、后台权益发放接口、全局任务工作台、管理员操作审计界面等，不要把现有占位页面当成这些功能。
- 不要只隐藏前端按钮；管理API必须后端强鉴权。已有role不等于已有可用的初始管理员配置流程，需设计受控初始化和越权测试。

## 5. 服务器真实状态

- 腾讯云轻量服务器：4核8GB，10Mbps，成都，Ubuntu22.04，x86_64，IP `129.28.26.221`，1Panel。
- 域名`vedio.vip`（注意拼写vedio），备案仍在审核；1Panel尚未创建网站，也未接公网HTTPS。
- PostgreSQL容器`1Panel-postgresql-fiYM`，postgres:16.15-alpine，宿主机只绑定127.0.0.1:5432。
- Redis容器`1Panel-redis-ey16`，redis:7.4.11，只绑定127.0.0.1:6379。
- OpenResty容器`1Panel-openresty-Tg8w`，network mode为host；可以代理宿主机127.0.0.1:8000。不能因此假定任意宿主机静态目录都已挂载到容器。
- 数据库`videohub_prod`，业务用户`videohub_user`，Alembic已到0007(head)。不要重建或清空。
- uv0.13.5、Python3.14.9、FFmpeg4.4.2已安装。
- 应用运行用户videohub；SSH部署账户videohub-deploy，22端口，SSH公钥登录已验证。
- Mac部署私钥路径`~/.ssh/videohub_actions`，不能输出/提交/索取其内容。
- 生产私有配置`/etc/videohub/backend.env`，root:videohub 0640。已有数据库/Redis/JWT等配置，发布不覆盖；其余业务配置需核实，不能假定都已填完。
- `/opt/videohub`已改为root所有，`current`指向`releases/SHA-唯一编号`；storage、Python、缓存复用现有目录。原手动上传backend仍保留。
- API systemd只监听127.0.0.1:8000；Worker单Solo。清理/恢复由独立systemd定时器执行。
- root辅助程序`/usr/local/sbin/videohub-deploy`，runtime工具`/usr/local/lib/videohub/runtime.py`；部署账户只能sudo调用此校验参数的辅助程序，不能任意sudo/docker。
- `/var/lib/videohub-deploy/backups/`为发布前同机pg_dump，`deployments.jsonl`记录发布过程；同机备份不能替代异机备份和恢复演练。

## 6. 已走通的CI/CD

用户希望学习企业式流程，采用适合单机小团队的版本交付，不引入Kubernetes。

流程：同步main→新开发分支→本地修改/测试→commit/push→PR→CI通过→合并main→自动发布包→手动选择CI run_id→Actions自动部署。

- `.github/workflows/ci.yml`：后端临时PG/Redis+FFmpeg，迁移及alembic check、pytest，禁止悄悄跳过集成测试；前端类型检查、测试、构建、试打包。无真实付费模型调用。
- main的检查成功后生成`videohub-release-完整SHA` Artifact，内部tar.gz和.sha256，release.json含SHA及各文件哈希；保留14天。
- `.github/workflows/deploy.yml`：手动workflow_dispatch，main分支，输入成功main CI运行的数字run_id；校验仓库、workflow、事件、结果与产物，不重新构建。
- GitHub Environment明确叫`production`；SSH_HOST/SSH_PORT/SSH_USER/SSH_PRIVATE_KEY/SSH_KNOWN_HOSTS已由用户配置在Environment Secrets，不是仓库Secrets。
- 首次CD已真实成功：已部署提交`5fa92bc953b39b47bde882add59653b55c615b91`，来源CI run `40204438832`。后端ready、Worker启动检查通过。
- 服务器部署辅助程序校验归档、安装锁定依赖、预检、停止写入服务/Worker正常收尾、备份PG、迁移、原子切current、健康检查、启动定时器。
- Worker最多等1200秒收尾，停止异常不继续迁移。首次无已验证旧版本失败则停服务；只有符合脚本兼容条件时切回旧代码，迁移失败/不兼容时保留现场，不自动回滚数据库。
- root辅助程序和systemd模板不随普通应用部署自行覆盖；修改这些基础设施后需管理员审阅并重新bootstrap安装。
- CI已验证前端API基址`/api/v1`，已修复测试写死localhost:8000的问题，补充相对/绝对API地址覆盖。
- 分支保护、生产审批保护规则是否启用尚未核实；创建Environment本身不等于已经启用审批。

## 7. 当前内部前端预览（容易混淆）

服务器已经有前端构建文件，但尚无网站静态服务。预览是下载服务器当前frontend/dist到Mac后本机托管；不是用本地源码开发页冒充服务器部署版本。

- 页面地址`http://127.0.0.1:5175/`。
- 页面静态文件在本地`.runtime/server-preview/dist`，来自服务器`/opt/videohub/current/frontend/dist`，不提交Git。
- `frontend/vite.server-preview.config.ts`只绑定127.0.0.1:5175，将/api/v1和/wechat_qr.png代理到127.0.0.1:18000。
- SSH隧道18000→服务器127.0.0.1:8000，使用videohub-deploy和上述Mac私钥；前端API真实连接生产服务器，实际创建AI任务会使用真实额度和供应商。
- 服务器更新后，本机静态文件是旧快照，需要重新下载并重启预览。
- `/`、`/subscription`、JS资源、经代理的health/ready和billing/catalog已检查200。真实登录/模型链路尚未因此验收。
- 页面不能绕过公众号生产登录；SSH不会让微信能访问本机回调。备案前只能内部使用，不假定公网回调已可用。
- 预览进程与隧道可能因会话结束停止；先检查端口，不要用旧PID盲目杀进程。

重新建立隧道（保持终端运行）：

```sh
ssh -i ~/.ssh/videohub_actions -o IdentitiesOnly=yes -o ExitOnForwardFailure=yes -N -L 127.0.0.1:18000:127.0.0.1:8000 videohub-deploy@129.28.26.221
```

重新下载静态文件（Mac项目根目录）：

```sh
mkdir -p .runtime/server-preview
scp -i ~/.ssh/videohub_actions -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -r videohub-deploy@129.28.26.221:/opt/videohub/current/frontend/dist .runtime/server-preview/
```

frontend目录启动：`node node_modules/vite/bin/vite.js preview --config vite.server-preview.config.ts`。

## 8. 当前未提交内容和最近验证

交接时main工作区已有两项未提交预览改动：frontend/README.md、frontend/vite.server-preview.config.ts。本文是新增交接文件。先检查git status，保留这些改动，不要reset/clean或误当管理功能提交。尚未为管理系统创建分支或编写业务代码。

- 最近完整后端：301 passed（含15项部署测试）。
- 最近前端：83 passed（相对和绝对API配置均通过），类型检查和生产构建通过。
- 内部预览配置另做TypeScript检查及真实只读接口/静态资源检查；没有为预览新增业务代码或调用AI。
- 新功能需要按改动重新验证，不能把历史结果当成新改动的通过证据。

## 9. 管理系统第一版建议（待确认）

建议沿用现有前端`/admin`和后端`modules/admin`契约，避免独立项目/独立认证/复杂RBAC平台。

1. 管理员初始化和后端权限：现有user/admin两级；受控初始化首个管理员；所有admin API鉴权、越权拒绝。前后端按钮一致。
2. 用户与权益：分页搜索、账号/关注/会员/额度状态；在既有发放契约上提供有原因、actor、幂等key的人工发放界面。暂不提供任意改余额、删除流水或模拟支付。
3. 全局任务工作台：按用户/状态/类型/时间筛选，错误码与耗时；取消仍走TaskService和CAS。管理员重试作为明确的新任务/报价流程设计，不能直接改状态或重跑收费子步骤。
4. 操作审计与基础概览：记录操作者、对象、原因、时间、请求/幂等键、结果；显示真实用户/任务/额度/存储统计。没有真实支付时不显示伪造营收，未知供应商成本标记未采集。
5. 健康/存储：复用已有健康和清理能力，遵守资源锁，不把网页后台做成任意Shell、SQL、密钥浏览器或无保护文件管理器。

先以只读查询和最小必要写入落地。套餐在线编辑、复杂角色、真实支付、自动退款、向量库、大屏、K8s等不自动扩入本阶段。管理员查看用户私有内容的范围需要明确，初版优先元数据和运维定位。

## 10. 正式上线仍待完成

ICP备案、DNS核实、1Panel建站、HTTPS、前端目录接入自动发布（当前仅stage）、正式公众号回调/接口权限、真实模型及下载链路/取消/额度结算验收、异机备份/恢复/告警。在线收费另需真实支付渠道与账务验收。后台开发不代表这些已经完成。

## 11. 新对话优先阅读的真实文件

- 根目录AGENTS.md、README.md。
- backend/README.md、STAGE9.md、ALLOWANCES_V1.md、KNOWLEDGE_V1.md、YOUTUBE_PRODUCTION.md。
- backend/deploy/CI.md、RELEASE.md、CD.md、build_release.py、deploy_release.py、runtime.py、bootstrap.sh和systemd模板。
- frontend/README.md、src/App.tsx、src/views/AdminView.tsx、SettingsView.tsx。
- backend/app/modules/auth/{models,dependencies,repository,service}.py。
- backend/app/modules/billing/{models,policy,repository,service,manage,router}.py。
- backend/app/modules/tasks/{models,repository,service}.py、modules/system/router.py，以及相关测试。
- .github/workflows/ci.yml和deploy.yml。

先完成现状核实与管理系统MVP设计，说明模块、API、审计、权限和验收，再按确认阶段实施。不要重新索取已配置的生产密码/密钥，也不要假定能直接用root远程操作服务器。
