import { defineConfig } from "vitest/config";

// Unit tests of the helpers. The Playwright specs in tests/ do not run here.
export default defineConfig({
  test: { environment: "node", include: ["lib/**/*.test.ts"] },
});
