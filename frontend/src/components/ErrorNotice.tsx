import type { ReactNode } from "react";
import type { ApiError } from "../api/client";
export default function ErrorNotice({
  error,
  children,
}: {
  error: ApiError | null;
  children?: ReactNode;
}) {
  if (!error) return null;
  return (
    <div className="error-notice" role="alert">
      <strong>{error.message}</strong>
      <code>{error.code}</code>
      {error.request_id && <small>请求编号：{error.request_id}</small>}
      {children}
    </div>
  );
}
