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
    },
  },
});
