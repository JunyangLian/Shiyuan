import { readFileSync } from "node:fs";
import { join } from "node:path";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

function devTokenPlugin() {
  return {
    name: "shiyuan-dev-token",
    configureServer(server: any) {
      server.middlewares.use("/__shiyuan_token", (_req: any, res: any) => {
        const base = process.env.SHIYUAN_HOME
          || join(process.env.LOCALAPPDATA || "", "ShiyuanData");
        try {
          const token = readFileSync(join(base, "api-token"), "utf8").trim();
          res.setHeader("Content-Type", "text/plain; charset=utf-8");
          res.setHeader("Cache-Control", "no-store");
          res.end(token);
        } catch {
          res.statusCode = 404;
          res.end("token not ready");
        }
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), devTokenPlugin()],
  clearScreen: false,
  test: {
    environment: "jsdom",
    globals: true,
    css: true,
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
  server: {
    port: 1420,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8737",
        changeOrigin: true,
      },
    },
  },
  envPrefix: ["VITE_"],
  build: {
    target: "chrome110",
    outDir: "dist",
  },
});
