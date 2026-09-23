import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
      "/readyz": "http://localhost:8000",
    },
  },
  preview: {
    port: 4173,
    proxy: {
      "/api": "http://backend:8000",
      "/healthz": "http://backend:8000",
      "/readyz": "http://backend:8000",
    },
  },
});
