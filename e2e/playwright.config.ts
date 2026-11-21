import { defineConfig, devices } from "@playwright/test";
import { DEFAULT_WEB_URL } from "./lib/env.ts";

// The suite runs against a deployed sama (staging by default). The tests share one test org,
// and the free plan allows 2 runs at the same time and 10 messages per minute for each user,
// so one worker runs the files in order.
export default defineConfig({
  testDir: "./tests",
  globalSetup: "./global-setup.ts",
  globalTeardown: "./global-teardown.ts",
  fullyParallel: false,
  workers: Number(process.env.E2E_WORKERS || 1),
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
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
