import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import wgsl from "@vgpu/wgsl/loader-vite";
import tailwindcss from "@tailwindcss/vite";
import publicPages from "./build/public-pages";
export default defineConfig({
  plugins: [react(), tailwindcss(), wgsl(), publicPages()],
  appType: "custom",
  server: { port: 5173, strictPort: true },
  test: { environment: "node", clearMocks: true },
  build: {
    manifest: "asset-manifest.json",
    chunkSizeWarningLimit: 650,
  },
});
