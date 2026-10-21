import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build/dev config only. Test (Vitest) config lives in `vitest.config.ts`,
// which extends this file — keeping the `test` block out of the production
// `tsc -b` build (vitest ships its own `vite` copy, so mixing the two config
// types here would clash).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Chat streaming lives on the separate chat service (compose: chat:8001).
      // This rule MUST come before "/api" so chat requests are not swallowed by
      // the API-service rule below. In AWS, CloudFront routes /api/chat to the
      // Chat Function URL; this is the local-dev equivalent.
      "/api/chat": {
        target: "http://localhost:8001",
        changeOrigin: true,
      },
      // Everything else under /api (datasets, ingest, history, suggestions, ...)
      // goes to the API service.
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      // Realtime WebSocket (useRealtime). This is the LOCAL-DEV equivalent of
      // the deployed VITE_WS_URL endpoint: in AWS the SPA build bakes
      // VITE_WS_URL to the API Gateway WebSocket URL and never uses this proxy;
      // locally VITE_WS_URL is unset, so useRealtime falls back to a same-origin
      // ws://localhost:5173/realtime, which this rule upgrades and forwards to
      // the compose `realtime-dev` relay on its host-published port 8002. Keep
      // this target in sync with the realtime-dev port in docker-compose.yml.
      "/realtime": {
        target: "ws://localhost:8002",
        ws: true,
      },
    },
  },
});
