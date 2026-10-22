import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";
export default defineConfig({
  plugins: [vue(), tailwindcss()],
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
