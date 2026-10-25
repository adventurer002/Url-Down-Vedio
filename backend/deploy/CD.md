# 第三步：手动触发生产部署（后端首版）

## 本阶段的边界

GitHub Environment名称为production，Secrets为SSH_HOST、SSH_PORT、SSH_USER、SSH_PRIVATE_KEY、SSH_KNOWN_HOSTS。部署账号videohub-deploy，与运行账号videohub分离。Secrets与服务器backend.env都不会进入发布包。

目前1Panel尚未创建网站。本阶段只启动本机8000端口后端和Worker，前端产物保留在版本目录，不发布公网网站。健康检查成功不代表模型、公众号回调或支付已验收。

## 一次性安装（由管理员执行）

此目录的bootstrap.sh、deploy_release.py、runtime.py和全部videohub-*.service/*.timer须作为一组上传。先审阅脚本，再在服务器root终端执行bootstrap.sh。不要直接把来自任意发布包的脚本当作root运行；后续根辅助程序固定安装在/usr/local/sbin，不由部署账号修改。

上传示例（Mac项目根目录）：

```sh
ssh -i ~/.ssh/videohub_actions -o IdentitiesOnly=yes videohub-deploy@129.28.26.221 'mkdir -p ~/bootstrap'
scp -i ~/.ssh/videohub_actions -o IdentitiesOnly=yes backend/deploy/bootstrap.sh backend/deploy/deploy_release.py backend/deploy/runtime.py backend/deploy/videohub-*.service backend/deploy/videohub-*.timer videohub-deploy@129.28.26.221:/home/videohub-deploy/bootstrap/
```

服务器root终端：

```sh
bash /home/videohub-deploy/bootstrap/bootstrap.sh
```

安装不会重写/etc/videohub/backend.env、现有数据库、Python或storage，也不会立即启动应用。它将/opt/videohub的目录所有者改为root，防止运行用户替换current版本指针；其内原有缓存、Python和storage仍归videohub使用。

固定sudo权限仅允许/usr/local/sbin/videohub-deploy，程序强制验证两个参数（完整Git SHA、包SHA256）。不会授予任意systemctl、bash、docker或全局sudo权限。部署账号也不应加入docker组或videohub组。

pg_dump通过现有1Panel-postgresql-fiYM容器内POSTGRES_USER本地连接备份videohub_prod。容器名称或库名变化时需管理员修改根辅助程序；备份失败则不会迁移。已有实例若禁用本地socket认证，先由管理员配置备份方式，不会把密码回显或传到Actions。

## 发起第一次部署

本次修改先PR检查并合并。等main的新CI运行成功并包含发布产物后，记下该CI运行URL末尾的数字run ID。

GitHub → Actions → Deploy production → Run workflow：分支选择main，run_id填写上述数字。只接受本仓库ci.yml在main成功完成的运行及其唯一未过期发布产物。使用完整SHA，不会重新构建或按模糊“最新版本”发布。

部署在GitHub串行运行，服务器另有flock防止重复并发。同版本成功部署后重复运行仅检查存活，不重复迁移。

## 发布步骤和目录

1. 校验下载包SHA256；上传到/home/videohub-deploy/incoming。
2. 根辅助程序验证tar所有路径、文件类型、大小、manifest与每个文件哈希；不允许软硬链接、路径穿越、隐藏文件。
3. 解包到/opt/videohub/releases/SHA-唯一编号，源文件root所有；以videohub身份安装锁定依赖、验证生产核心配置及PG/Redis连接。
4. 停止清理/恢复定时器与API；Worker通过SIGTERM正常收尾，最多1200秒。若被强杀或停止异常，停止发布，检查进程组，不继续迁移。
5. 创建root私有/var/lib/videohub-deploy/backups/数据库备份，执行alembic upgrade head。
6. 原子切换/opt/videohub/current符号链接；启动API和Worker，检查ready与Worker启动日志，再恢复定时器。

业务配置占位字段会显示待完善项目名称，不输出值。首次仅在本机验收，填写完整并实测模型、登录后才能对外使用。服务原生运行，保持Solo -c 1；账号并发额度不代表实际三路执行。

首次没有旧版本时失败会停止服务；原先手动上传的backend仍保留，但不会作为已验证回滚版本。后续仅在迁移文件完全相同且迁移成功、服务能正常停止时，才自动切回上个版本；迁移失败、迁移文件改变后的启动失败或停止异常保留备份和现场，需人工恢复。不能自动回滚数据库。

## 验收和排查

```sh
curl --fail --max-time 3 http://127.0.0.1:8000/api/v1/health/ready
systemctl status videohub-api videohub-worker --no-pager
systemctl list-timers 'videohub-*'
readlink -f /opt/videohub/current
sudo tail -n 10 /var/lib/videohub-deploy/deployments.jsonl
```

日志在journalctl -u videohub-api/-u videohub-worker，失败状态在deployments.jsonl。服务准备或迁移有显式超时。源码更新不覆盖.env。根辅助程序/服务模板自身更新仍需管理员重新审阅安装，普通部署不能自行提权更新它们。

如果Actions连接超时，核实云防火墙与主机防火墙是否允许该runner访问22；GitHub托管runner出口可能变化。不要关闭主机指纹校验来处理连接错误。网络中断或强制中止发布时，先检查部署日志、current、服务和备份，再重试，避免在数据库状态未知时盲目切版本。

自动发布备份是同机恢复点，不是异机备份。仍需面板每日备份、异机复制和恢复演练。当前不自动清理版本和备份，初期人工保留近期版本并监控磁盘；发布前至少需4GiB空闲，下载临时峰值继续由存储限额保护。迁移期间API短暂停机，这是单机首版的明确限制。

## 本地验证

在backend目录执行uv run pytest；部署专项测试覆盖包篡改、路径穿越、链接、大小限制、停止/备份/迁移顺序、幂等重发、迁移失败保留现场、同schema启动失败回滚及Worker停止超时。脚本bash -n、Python编译、YAML结构也需通过。测试不连接真实服务器或调用付费模型。本阶段没有新增HTTP接口。
