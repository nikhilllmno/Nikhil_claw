import { defineConfig, devices } from "@playwright/test";
import { DEFAULT_WEB_URL } from "./lib/env.ts";

/**
 * The spec files that need many sandboxes or long sandbox work: code in the sandbox, coding
 * agents, coding mode, script tools, and hosted MCP servers. Each chat session keeps its E2B
 * sandbox for 15 minutes after its last run (architecture Section 16.2), and the E2B account
 * allows 20 sandboxes at one time. So these files run in their own project with one worker,
 * and the whole suite has at most 2 workers, so a pass needs only a few sandboxes at once.
 */
export const SANDBOX_HEAVY = [
  /03-approvals-steer\.spec\.ts/,
  /09-connections\.spec\.ts/,
  /15-coding-agents\.spec\.ts/,
  /19-custom-tools\.spec\.ts/,
  /21-coding-mode\.spec\.ts/,
];

/** At most 2 workers: the free plan allows 2 runs at the same time, and E2B 20 sandboxes. */
const workers = Math.min(2, Math.max(1, Number(process.env.E2E_WORKERS || 1)));

// The suite runs against a deployed sama (staging by default). The tests share one test org.
export default defineConfig({
  testDir: "./tests",
  globalSetup: "./global-setup.ts",
  globalTeardown: "./global-teardown.ts",
  fullyParallel: false,
  workers,
  forbidOnly: !!process.env.CI,
  // A model can be slow. One retry tells a flaky answer from a broken flow.
  retries: process.env.CI ? 1 : 0,
  // An agent run can take minutes.
  timeout: 6 * 60_000,
  expect: { timeout: 20_000 },
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never" }], ["junit", { outputFile: "test-results/junit.xml" }]]
    : [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.E2E_WEB_URL || DEFAULT_WEB_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 20_000,
    navigationTimeout: 45_000,
    ...devices["Desktop Chrome"],
  },
  projects: [
    { name: "chromium", testIgnore: SANDBOX_HEAVY },
    {
      name: "sandbox-heavy",
      testMatch: SANDBOX_HEAVY,
      // No dependency on the first project: a failure there must not skip these files.
      // One file at a time, also when E2E_WORKERS is 2.
      fullyParallel: false,
      workers: 1,
    },
  ],
});
