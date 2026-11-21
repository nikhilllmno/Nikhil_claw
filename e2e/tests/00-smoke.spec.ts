// Every page of the web app opens for the owner of the test org and shows its heading.

import { expect, test } from "../fixtures.ts";

const PAGES: [string, string][] = [
  ["/", "What should we work on?"],
  ["/tasks", "Task board"],
  ["/jobs", "Jobs"],
  ["/search", "Search"],
  ["/memory", "Memory"],
  ["/skills", "Skills"],
  ["/connections", "Connections"],
  ["/members", "Members"],
  ["/billing", "Billing"],
  ["/settings", "Settings"],
  ["/admin", "Admin"],
  ["/onboarding", "Set up"],
];

test("the API and the web app are up", async ({ request, world }) => {
  const health = await request.get(`${world.apiUrl}/healthz`);
  expect(health.status()).toBe(200);
  const config = await request.get(`${world.apiUrl}/config`);
  expect(config.status()).toBe(200);
  expect((await config.json()).signup).toBe("open");
});

test("each page of the shell opens", async ({ ownerPage: page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  for (const [path, heading] of PAGES) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: heading }).first()).toBeVisible();
    // The route announcer of Next.js is an empty alert, so look for the error alerts of the app.
    await expect(page.locator(".alert-error")).toHaveCount(0);
  }
  expect(errors).toEqual([]);
});

test("the agent page shows the settings of the agent", async ({ ownerPage: page, world }) => {
  await page.goto(`/agents/${world.org.profileId}`);
  await expect(page.getByRole("heading", { name: "General" })).toBeVisible();
  await expect(page.getByLabel("Runtime")).toBeVisible();
  await expect(page.getByLabel("Model")).toBeVisible();
});
