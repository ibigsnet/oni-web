import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@web": fileURLToPath(new URL("./src/web", import.meta.url)) } },
  server: {
    port: 5173, strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8000", ws: true } },
  },
  build: { target: "es2022", chunkSizeWarningLimit: 1500 },
  test: { environment: "node" },
});
