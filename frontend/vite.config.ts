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
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
