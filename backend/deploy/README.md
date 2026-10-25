# Ubuntu 22.04 + 1Panel 首次部署准备

适用：4核8GB、x86_64，复用面板 PostgreSQL16 / Redis7 / OpenResty；API 与单个 Solo Worker 运行在宿主机。当前文件是部署模板，尚未执行远程部署。

## 先确认

- 在面板创建专用 `videohub_prod` 数据库和用户；核实 Redis 认证方式，只在服务器填写密码。
- 查询 OpenResty 网络模式：`docker inspect --format '{{.Name}} mode={{.HostConfig.NetworkMode}}' 1Panel-openresty-Tg8w`。
- 此模板 API 只监听 127.0.0.1。OpenResty 为 host 网络才能直接代理此地址；若为 bridge，先确认网络与宿主机连接方式再调整监听和防火墙。不要直接开放8000公网端口。
- 备案完成、DNS、HTTPS、公众号接口权限及真实模型 ID 核实后才能进行公网业务验收。模型名称不能凭产品昵称猜测。付款功能继续关闭。

## 文件和运行账户

创建系统用户/组 `videohub`，上传项目到 `/opt/videohub`。上传源代码和锁文件，不上传 `.env`、`.git`、`.venv`、`node_modules`、`storage`、`.runtime` 或本地日志。保留 `frontend/public/wechat_qr.png`（如使用本地公众号二维码）。

安装系统 FFmpeg 和 uv（按各自官方安装说明）；Python要求3.14。用 videohub 用户执行：

```sh
cd /opt/videohub/backend
UV_PYTHON_INSTALL_DIR=/opt/videohub/.python uv python install 3.14
UV_PYTHON_INSTALL_DIR=/opt/videohub/.python uv sync --frozen --no-dev
```

确保 `/opt/videohub/.python`、虚拟环境及 storage 均可由服务账户读取/写入。不要使用 /root 下的 Python。不要上传本地 Mac 虚拟环境。

把 production.env.example 复制为 `/etc/videohub/backend.env`，填写实际配置；目录 root:videohub 0750、文件 root:videohub 0640。删除上传项目中的开发 `.env`，避免遗漏配置回落到开发环境。密码含特殊字符时先进行 URL 编码。

## 数据库和启动

先备份数据库。首次迁移使用与服务相同的环境文件，例如 root 在一次性 shell 中执行（配置按模板 shell 语法填写）：

```sh
set -a
. /etc/videohub/backend.env
set +a
cd /opt/videohub/backend
runuser -u videohub --preserve-environment -- .venv/bin/alembic upgrade head
```

复制本目录 .service/.timer 到 /etc/systemd/system 后：

```sh
systemctl daemon-reload
systemctl enable --now videohub-api videohub-worker videohub-cleanup.timer videohub-reconcile.timer
curl --fail --max-time 3 http://127.0.0.1:8000/api/v1/health/ready
systemctl status videohub-api videohub-worker --no-pager
systemctl list-timers 'videohub-*'
```

Worker配置为实际串行执行；会员3个任务额度不意味着服务器三路并行。停止Worker使用普通 systemctl stop，预留最多1200秒正常收尾。修改任务超时时同步增加停止超时。

## 前端和代理

本机 frontend 目录用 `VITE_API_BASE_URL=/api/v1 pnpm build`，把 dist 内容上传到面板实际创建的网站根目录。OpenResty 容器看不到任意宿主机目录，不要猜测映射路径。

网站需配置 SPA 回退 `try_files $uri $uri/ /index.html`；`/api/v1/` 代理到确认可达的后端，保留原始路径，设置 HTTP/1.1、Host、X-Forwarded-Proto，关闭 proxy_buffering/proxy_cache 并设 proxy_read_timeout 120s。SSE不能缓冲。公众号静态二维码路径 `/wechat_qr.png` 可由前端静态目录提供。服务器已确认 OpenResty 为 host 网络，可使用本目录 openresty-api.conf 中的代理配置；放在网站 server 块内。

## 验收与维护

检查健康就绪、真实公众号登录、短视频下载、字幕/ASR、总结导图、问答、历史结果取回、取消与重启恢复；真实模型测试由操作员控制费用。失败/取消核对额度释放，检查已完成任务不重复扣费。确认匿名和普通用户不能调用全局清理接口。

1Panel 配置每日 PostgreSQL备份，至少保留7天并复制到服务器外；首次上线前实际恢复一次。Redis保持无淘汰策略，监控内存与磁盘；磁盘统计需包括合并/转码临时峰值。定时器失败查 journalctl，恢复任务遇到仍存活的孤儿进程组会保守保留冻结，需人工确认，不能盲目释放。当前尚无自动告警，需要定期检查容量、失败任务与备份。

升级前备份数据库，保留上一版源代码和静态资源；数据库有新迁移时不能保证仅回滚文件即可恢复。此方案未接入OSS或真实支付。
