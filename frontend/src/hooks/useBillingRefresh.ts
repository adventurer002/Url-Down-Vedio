import { useEffect, useState } from "react";
import { useAuth } from "../stores/auth";
import { billingStore, useBilling } from "../stores/billing";

/** Keep the public account snapshot fresh without polling hidden tabs. */
export function useBillingRefresh() {
  const auth = useAuth();
  const billing = useBilling();
  const [visible, setVisible] = useState(document.visibilityState !== "hidden");
  const active =
    !auth.restoring && !auth.restoreError && auth.user?.status === "active";

  useEffect(() => {
    if (!active) return;
    const refresh = () => {
      const shown = document.visibilityState !== "hidden";
      setVisible(shown);
      if (shown) void billingStore.refresh({ ifStale: true });
    };
    refresh();
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("focus", refresh);
    window.addEventListener("online", refresh);
    return () => {
      document.removeEventListener("visibilitychange", refresh);
      window.removeEventListener("focus", refresh);
      window.removeEventListener("online", refresh);
    };
  }, [active, auth.user?.id, auth.user?.is_subscribed]);

  useEffect(() => {
    if (!active || !visible || billing.loading) return;
    // Failed requests retain the last snapshot and back off before retrying.
    const delay =
      billing.error || !billing.data
        ? 60_000
        : Math.max(50, billing.freshUntil - Date.now() + 50);
    const timer = window.setTimeout(() => {
      if (document.visibilityState !== "hidden")
        void billingStore.refresh({ ifStale: true });
    }, delay);
    return () => window.clearTimeout(timer);
  }, [
    active,
    visible,
    billing.loading,
    billing.error,
    billing.data,
    billing.freshUntil,
  ]);
}
