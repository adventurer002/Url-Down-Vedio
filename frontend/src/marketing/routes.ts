export const marketingPaths = ["/", "/help", "/privacy", "/examples"] as const;
export const documentPaths = [...marketingPaths, "/subscription"] as const;

export function cleanPath(path: string) {
  return path === "/" ? path : path.replace(/\/+$/, "");
}
export function isMarketingPath(path: string) {
  const normalized = cleanPath(path);
  return (
    marketingPaths.some((item) => item === normalized) ||
    normalized.startsWith("/examples/")
  );
}
export function isWorkspacePath(path: string) {
  return (
    ["/app", "/account", "/library", "/settings", "/subscription"].includes(
      cleanPath(path),
    ) ||
    /^\/video\/[^/]+\/?$/.test(path) ||
    /^\/admin(?:\/.*)?$/.test(path)
  );
}
