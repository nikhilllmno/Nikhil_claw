import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig([
  globalIgnores(["node_modules/**", "playwright-report/**", "test-results/**", ".state/**"]),
  js.configs.recommended,
  ...tseslint.configs.recommended,
  { languageOptions: { globals: { ...globals.node } } },
  {
    rules: {
      // A test that waits a fixed time is slow and flaky. Wait for a condition instead.
      "no-restricted-properties": [
        "error",
        { object: "page", property: "waitForTimeout", message: "Wait for a condition, not a time." },
      ],
      // Secrets must not reach the logs (AGENTS.md).
      "no-console": ["error", { allow: ["warn", "error"] }],
    },
  },
]);
