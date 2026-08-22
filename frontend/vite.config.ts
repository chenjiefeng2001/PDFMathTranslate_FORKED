import { readFileSync } from "node:fs";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const pkg = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf-8")) as {
  version: string;
};

// Tauri v2 健壮性约束：
// 1. base: "./" —— 产物使用相对路径，可从任意 origin（tauri://、file://、子路径）加载；
// 2. /api 代理仅服务开发期；生产由 FastAPI StaticFiles 或 Tauri sidecar 提供同源/注入地址。
export default defineConfig({
  plugins: [react()],
  base: "./",
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:11009",
    },
  },
});
