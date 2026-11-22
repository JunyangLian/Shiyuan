import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
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
