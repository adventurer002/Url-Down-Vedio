# 第一步：持续集成（CI）

工作流：`.github/workflows/ci.yml`。本阶段只检查代码，不发布到服务器，不需要 GitHub Secrets。

## 保留已有服务器环境

保留 /opt/videohub、videohub 用户、Python/uv/FFmpeg、1Panel 服务、videohub_prod 数据库（0007）以及 /etc/videohub/backend.env。不要再次复制模板覆盖已经填写的配置。未完成的公众号/模型配置仍需在首次业务验收前完成。

后续 CD 阶段将接管版本目录、服务启动与发布，不再手动上传源码；现阶段不移动服务器文件、不重新迁移生产数据库。

## 自动检查内容

- main 分支 push、目标为 main 的 PR、手动 Run workflow 都会触发。
- 后端临时 PostgreSQL16 / Redis7，只存在于 GitHub runner；CI 密码是公开的临时测试值，不是生产凭据。锁定 Python3.14 与 uv，安装 FFmpeg，执行迁移、模型一致性检查及完整测试。跳过测试会使 CI 失败，防止集成测试悄悄缺席。
- 前端按 package.json 的 pnpm 版本和锁文件安装，使用 Node22，执行类型检查、单元测试及生产构建，API 基址为 /api/v1。
- AI 使用离线测试替身，公众号使用 mock。CI 通过不代表真实供应商、公众号公网回调和生产网络已验收。
- 工作流仅有仓库读取权限，无 SSH、发布、生产凭据。

## 第一次在 GitHub 验证

本地所有新增业务文件（包含0006/0007迁移和相应测试）需要随本次基线一起纳入版本控制；只提交 workflow 而漏掉这些文件会导致仓库与已部署数据库不匹配。提交前检查差异，不提交 .env、运行日志、缓存、虚拟环境、视频或密钥。

当前有已跟踪的 .pnpm-store 缓存。新增忽略规则只能阻止新的缓存进入Git；需另行从Git索引移除已跟踪项，保留本机文件。

建议在开发分支提交并推送，向main创建PR；在PR的Checks或仓库Actions页查看 Backend tests and migrations / Frontend tests and build。两项都成功后合并，main会再次检查。首次 GitHub 运行仍需实际观察，不能用本地测试替代。

CI首次通过后，再根据仓库可用功能配置main分支保护：要求PR及这两项检查通过。单人项目可暂不强制他人审批，避免无法合并。工作流文件本身不会自动开启分支保护。

## 本地等价检查

backend目录：uv run pytest；使用专门测试数据库时还可运行 uv run alembic upgrade head 和 uv run alembic check，切勿把测试数据库变量指向生产。

frontend目录：pnpm type-check、pnpm test:unit、VITE_API_BASE_URL=/api/v1 pnpm build。

本阶段无新增服务或接口，无需启动API或用curl验收。下一阶段才准备发布产物与手动生产部署。
