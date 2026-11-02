# vedio.vip 网站接入与前端随部署更新

2026-11-03：备案已通过；1Panel站点与SSL已创建，真实宿主目录为/opt/1panel/www/sites/vedio.vip/index。容器已挂载/opt/1panel/www为/www；API在宿主127.0.0.1:8000，OpenResty为host网络。正式域名ready已验证成功。线上仍为5fa92bc旧SPA版本，本地管理系统等业务修改尚未提交，不等于已经上线。

## 前端发布机制

部署辅助程序增加publish_frontend：仅把manifest中已校验的frontend/dist文件复制到站点内.videohub-releases/版本目录。SSL、Nginx配置、backend.env和私有存储不受影响。不向容器增加挂载，不把index链接到容器看不到的/opt/videohub。

首次将1Panel的index目录保留为站点下.videohub-initial-index-编号，再建立index相对链接。首次转换有极短的目录切换间隙，后续符号链接替换为原子操作。每次后端就绪后发布前端；后端符合自动回退条件时同时恢复前端。已成功版本重复部署也会修复前端发布，而不重启或迁移数据库。

为已打开页面保留前版带哈希文件名的assets，累计上限128MiB，超过需人工检查清理。完整版本和初始目录也不自动删除，继续监控磁盘。源文件校验失败、非法链接或写入失败会使发布失败，而不会当成成功。

没有创建站点时保持原先仅暂存前端的行为。站点名称目前固定vedio.vip，根辅助程序需由管理员审阅安装，不从应用包自动自我更新。

## 一次性安装更新后的辅助程序

该脚本的改动需独立审查。当前分支还有大量业务改动，不能使用git add .将其一并部署。先提交部署相关文件并通过CI，再由管理员更新/usr/local/sbin/videohub-deploy（或重新执行已审阅的bootstrap）。之后可重跑当前已成功CI run的部署来启用站点链接，旧版本不需迁移或重启。

Mac项目根目录上传：

```sh
scp -i ~/.ssh/videohub_actions -o IdentitiesOnly=yes backend/deploy/deploy_release.py videohub-deploy@129.28.26.221:/home/videohub-deploy/bootstrap/deploy_release.py
```

服务器root安装：

```sh
install -o root -g root -m 0755 /home/videohub-deploy/bootstrap/deploy_release.py /usr/local/sbin/videohub-deploy
```

Nginx目前使用已部署旧SPA的路由回退。未来发布本地新预渲染页面版本时，需要一起审核其openresty-api.conf路由和上传规则，不能假定当前旧SPA配置已符合新版本SEO/404/上传行为。

## 验收

- 每次部署日志显示frontend published，Actions绿色不再只代表后端运行。
- readlink -f /opt/1panel/www/sites/vedio.vip/index指向同站点.videohub-releases下。
- https://vedio.vip/、/subscription及JS/CSS资源可获取；/api/v1/health/ready为200 JSON。
- 比较线上index.html和对应发布目录frontend/dist/index.html的SHA256；发布后保持一致。
- 新版本页面获取成功、旧哈希资源仍可读；符合条件的回退后页面与后端回到旧版本。

本地专项测试使用临时目录，不修改真实站点或调用AI。全量测试仍需在backend目录用uv run pytest执行。此变更没有新增HTTP接口或依赖。
