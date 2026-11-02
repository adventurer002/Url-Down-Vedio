export function pageLabel(pathname: string) {
  if (pathname === "/admin" || pathname === "/admin/") return "管理概览";
  if (pathname.startsWith("/admin/tasks/")) return "任务详情";
  if (pathname.startsWith("/admin/users/")) return "用户详情";
  if (pathname.startsWith("/admin/allowances/")) return "用户权益";
  if (pathname.startsWith("/admin/tasks")) return "任务管理";
  if (pathname.startsWith("/admin/users")) return "用户管理";
  if (pathname.startsWith("/admin/allowances")) return "会员与额度";
  if (pathname.startsWith("/admin/audit")) return "操作审计";
  if (pathname.startsWith("/admin/system")) return "存储与服务";
  if (pathname.startsWith("/admin/")) return "管理控制台";
  if (pathname.startsWith("/video/")) return "内容工作台";
  return (
    (
      {
        "/": "首页",
        "/app": "内容工作台",
        "/account": "个人中心",
        "/library": "我的知识库",
        "/subscription": "订阅方案",
        "/settings": "设置",
        "/help": "使用帮助",
        "/privacy": "隐私说明",
      } as Record<string, string>
    )[pathname] ?? "首页"
  );
}
