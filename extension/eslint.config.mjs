import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig([
  globalIgnores(["dist/**", "node_modules/**"]),
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.ts"],
    languageOptions: { globals: { ...globals.browser, ...globals.serviceworker, chrome: "readonly" } },
    rules: {
      "no-restricted-properties": [
        "error",
        { object: "document", property: "write", message: "Build the DOM with the helpers." },
      ],
      "no-restricted-syntax": [
        "error",
        {
          selector: "AssignmentExpression[left.property.name='innerHTML']",
          message: "Do not set innerHTML: page and agent text is untrusted. Use textContent.",
        },
      ],
    },
  },
  { files: ["*.mjs", "*.mts"], languageOptions: { globals: globals.node } },
]);
