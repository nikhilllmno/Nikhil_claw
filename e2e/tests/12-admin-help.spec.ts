// M6.9: the admin area for an org owner, the data controls in the web app (M6.8), and the help
// pages, which need no sign-in.

import { readFile } from "node:fs/promises";
import type { Page } from "@playwright/test";
import zip from "../lib/zip.ts";
import { expect, signInPage, test } from "../fixtures.ts";

const ADMIN_PAGES: [string, string][] = [
  ["/admin/usage", "Usage"],
  ["/admin/audit", "Audit log"],
  ["/admin/changes", "Agent changes"],
  ["/admin/curator", "Curator reports"],
  ["/admin/keys", "Org keys and region"],
  ["/admin/data", "Data controls"],
];

/** Clicks a download button and returns the names in the zip file. */
async function downloadZip(page: Page, button: string | RegExp): Promise<string[]> {
  const download = page.waitForEvent("download", { timeout: 120_000 });
  await page.getByRole("button", { name: button }).click();
  return zip.names(await readFile(await (await download).path()));
}

test("each admin page opens for the owner", async ({ ownerPage: page }) => {
  await page.goto("/admin");
  await expect(page.getByRole("heading", { level: 1, name: "Admin" })).toBeVisible();
  for (const [path, heading] of ADMIN_PAGES) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    await expect(page.locator(".alert-error")).toHaveCount(0);
  }
  // The audit log has the creation of the org.
  await page.goto("/admin/audit");
  await expect(page.getByRole("region", { name: "Audit entries" }).getByRole("listitem").first()).toBeVisible();
});

test("download the export of the org and of my data", async ({ ownerPage: page }) => {
  await page.goto("/admin/data");
  const org = await downloadZip(page, "Download the export");
  expect(org).toContain("manifest.json");
  expect(org.some((n) => n.startsWith("tables/"))).toBe(true);

  await page.goto("/settings");
  const mine = await downloadZip(page, "Download my data");
  expect(mine).toContain("manifest.json");
});

test("a member cannot open the admin area", async ({ browser, world, tokenCache, memberApi }) => {
  const joined = (await memberApi.me()).orgs.some((o) => o.id === world.org.id);
  test.skip(!joined, "The member joins the org in 06-members.");
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    await signInPage(page, await tokenCache.forUser("member"), world.org.workspaceId);
    await page.goto("/admin/data");
    await expect(page.getByRole("heading", { name: "Only for org admins" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Download the export" })).toHaveCount(0);
  } finally {
    await context.close();
  }
});

test("the help pages open without a sign-in", async ({ page }) => {
  await page.goto("/help");
  await expect(page.getByRole("heading", { level: 1, name: "Help" })).toBeVisible();
  await page.getByRole("link", { name: /Getting started/ }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Getting started" })).toBeVisible();
});
