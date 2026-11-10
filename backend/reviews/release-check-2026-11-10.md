# 2026-11-10 发布检查

范围：当前落地页、白色卡片层次、订阅方案展示与相关公开文档、静态输出。

## 检查结论

- 后端：`cd backend && uv run pytest`，833 passed，334.29 秒。
- 前端：`cd frontend && pnpm exec vitest run --maxWorkers=2`，29 个文件、351 项全部通过。
- 类型：`pnpm type-check` 通过。
- 生产构建：`VITE_API_BASE_URL=/api/v1 VITE_SITE_URL=https://vedio.vip pnpm build` 通过。
- 构建产物校验：5 个独立公开 HTML、SEO 元数据、静态资源、站点地图和私有页面 noindex 通过。
- `git diff --check` 通过。
- 当前后端应用、数据库迁移与依赖与线上 085b0a1 版本一致。

## 发布前修复

订阅预告页此前可以跳转真实外部收款页，但自动核款及权益交付尚未接通。依据现有金数据核验结论，关闭全部会员周期的付款跳转并明确显示暂未开放购买。保留价格展示、已获权益和个人中心入口。六种套餐周期均有关闭付款入口的回归检查。

## 部署方式

使用现有 SSH 部署账号和固定 `/usr/local/sbin/videohub-deploy`，不更换服务器配置、凭据或数据库连接。发布程序先校验包和文件摘要、预检，再正常停止 Worker、备份数据库、检查迁移、切换 current，验证 API 与 Worker 后发布前端，并保留回滚版本。

## 验证与启动

本地前端：`cd frontend && pnpm dev --port 5174`。
正式页面：`https://vedio.vip/`、`/subscription`、`/app`。
健康验证：`curl --fail --max-time 5 https://vedio.vip/api/v1/health/ready`。
服务器服务检查：`systemctl is-active videohub-api videohub-worker`。

本次单元与集成测试使用测试环境；不以其替代真实付费模型任务或支付验收。此次上线保持在线付款关闭。

## 修改文件

- `backend/reviews/jinshuju-payment-verification.md`
- `backend/reviews/payment-integration-plan.md`
- `frontend/build/verify-public.mjs`
- `frontend/public/media/landing/hero-composite-meditation.webp`
- `frontend/src/account.css`
- `frontend/src/admin/admin.css`
- `frontend/src/content-workspace.css`
- `frontend/src/knowledge.css`
- `frontend/src/marketing/LandingHero.tsx`
- `frontend/src/marketing/MarketingSite.tsx`
- `frontend/src/marketing/ProductShowcase.tsx`
- `frontend/src/marketing/content.ts`
- `frontend/src/marketing/hero.css`
- `frontend/src/marketing/landing-experience.css`
- `frontend/src/marketing/marketing.css`
- `frontend/src/marketing/render.tsx`
- `frontend/src/marketing/routes.ts`
- `frontend/src/marketing/showcase.css`
- `frontend/src/marketing/useLandingMotion.ts`
- `frontend/src/pricing.css`
- `frontend/src/style.css`
- `frontend/src/subscription.css`
- `frontend/src/subscriptionOffers.ts`
- `frontend/src/tests/marketing.test.tsx`
- `frontend/src/tests/subscription.test.tsx`
- `frontend/src/views/SubscriptionView.tsx`
