import { useEffect, useState } from "react";

export function App() {
  const [ready, setReady] = useState<string>("checking...");

  useEffect(() => {
    fetch("/readyz")
      .then((r) => r.json())
      .then((j) => setReady(JSON.stringify(j)))
      .catch((e) => setReady(`backend unreachable: ${String(e)}`));
  }, []);

  return (
    <main style={{ maxWidth: 640, margin: "80px auto", padding: 24, fontFamily: "sans-serif" }}>
      <h1>down-vedio</h1>
      <p>Phase 1 skeleton. Backend status:</p>
      <pre style={{ background: "#f4f4f4", padding: 12, whiteSpace: "pre-wrap" }}>{ready}</pre>
    </main>
  );
}
