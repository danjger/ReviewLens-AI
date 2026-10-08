import { defineConfig, mergeConfig } from "vitest/config";

import viteConfig from "./vite.config";

// Vitest config, kept separate from `vite.config.ts` so the production build's
// `tsc -b` never type-checks the `test` block (vitest bundles its own `vite`
// copy, whose config type differs from the top-level `vite`). It reuses the
// build config via `mergeConfig` so the React plugin and proxy stay in sync.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      globals: true,
      environment: "jsdom",
      setupFiles: "./tests/setup.ts",
      coverage: {
        provider: "v8",
        reporter: ["text", "json", "html"],
        include: ["src/**/*.{ts,tsx}"],
      },
    },
  }),
);
