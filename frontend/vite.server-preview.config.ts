import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// Local-only preview of the deployed static files, through the operator's SSH tunnel.
export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  build: {
    outDir: fileURLToPath(new URL("../.runtime/server-preview/dist", import.meta.url)),
  },
  preview: {
    host: "127.0.0.1",
    port: 5175,
    strictPort: true,
    proxy: {
      "/api/v1": {
        target: "http://127.0.0.1:18000",
        changeOrigin: true,
        timeout: 120000,
        proxyTimeout: 120000,
      },
      "/wechat_qr.png": {
        target: "http://127.0.0.1:18000",
        changeOrigin: true,
        timeout: 10000,
        proxyTimeout: 10000,
      },
    },
  },
});
