import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, strictPort: true },
  test: { environment: "node", clearMocks: true },
  build: {
    chunkSizeWarningLimit: 650,
    rollupOptions: {
      output: {
        manualChunks: {
          "markmap-parser": ["markmap-lib"],
          "markmap-renderer": ["markmap-view"],
        },
      },
    },
  },
});
