import { isMarketingPath, isWorkspacePath } from "./marketing/routes";

// Public pages start without the workspace, auth store, GPU or document engines.
if (
  isMarketingPath(window.location.pathname) ||
  !isWorkspacePath(window.location.pathname)
) {
  void import("./marketing/client");
} else {
  void import("./workspace-client");
}
