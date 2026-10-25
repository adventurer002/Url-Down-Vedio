# 第二步：版本发布包

CI前后端检查均成功后，main分支push或main上手动运行工作流才执行Package tested release。PR会试打包以验证脚本，但不会上传正式发布包。

前端只构建一次，打包任务下载同一次工作流的产物。后端从同一完整提交SHA读取已提交源文件。包名与release.json记录完整SHA。

## 下载和内容

GitHub仓库 → Actions → 对应main的CI运行 → Artifacts → videohub-release-完整SHA。下载得到GitHub的ZIP封装，解压后包含tar.gz发布包及同名.sha256校验文件，Linux可执行 sha256sum -c 文件名.sha256 验证。

包内有backend/app、迁移、依赖锁、部署文件、frontend/dist，以及release.json（提交SHA、各文件SHA256、API基址）。也保留后端现有二维码路由所需的frontend/public/wechat_qr.png（构建中存在时）。

不包含真实.env、虚拟环境、node_modules、数据库、下载文件、日志或测试目录。deploy/production.env.example是占位模板，不可覆盖服务器已有配置。源文件来自Git，因此任何真实凭据都不应提交到Git。

后端是源码交付，需要服务器用uv sync --frozen --no-dev安装Linux依赖；FFmpeg、Python、数据库复用现有安装。生成包不代表部署或公网验收完成。

发布产物保留14天，中间前端产物7天。Artifact用于初期部署交接，不是长期备份或GitHub Release。后续部署需记录准确run ID、SHA并校验，不能随意选不同版本的“最新”产物。

## 本地验证

在frontend构建生产静态文件，再在项目根目录执行：

```sh
python3 backend/deploy/build_release.py --commit "$(git rev-parse HEAD)" --frontend-dist frontend/dist --output-dir /tmp/videohub-release-check
```

本地仅验证打包机制，前端必须对应指定提交。CI通过同一checkout与同次工作流产物保证。未提交后端修改不会进入指定提交的包。

本阶段无需SSH密钥或服务器操作，无新增HTTP接口。合并后观察main的第三项Package tested release是否成功，下载核对manifest；PR中此项Skipped是预期行为。
