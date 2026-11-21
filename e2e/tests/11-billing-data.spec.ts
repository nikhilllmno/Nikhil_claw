// M6.6: the Billing page and a Stripe Checkout in test mode. The test does not pay: it checks
// that Checkout opens in test mode. M6.8: the export API (the pages are in 12-admin-help).

import zip from "../lib/zip.ts";
import { expect, test } from "../fixtures.ts";

test("the Billing page shows the plans and opens Stripe Checkout", async ({ ownerPage: page }) => {
  await page.goto("/billing");
  const plans = page.getByRole("region", { name: "Plans" });
  await expect(plans.getByText("Free").first()).toBeVisible();
  await expect(plans.getByText("Current")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Usage and quotas" })).toBeVisible();

  await page.getByRole("button", { name: "Upgrade to Pro" }).click();
  await page.waitForURL(/checkout\.stripe\.com/, { timeout: 60_000 });
  // A test-mode Checkout session id starts with cs_test_.
  expect(page.url()).toContain("cs_test_");
});

test("export the org and the user as zip files", async ({ ownerApi, world }) => {
  for (const path of [
    `/orgs/${world.org.id}/export`,
    `/orgs/${world.org.id}/users/${(await ownerApi.me()).orgs.find((o) => o.id === world.org.id)!.user_id}/export`,
  ]) {
    const response = await fetch(`${world.apiUrl}${path}`, {
      headers: { authorization: `Bearer ${await ownerApi.accessToken()}` },
    });
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("zip");
    const names = zip.names(Buffer.from(await response.arrayBuffer()));
    expect(names).toContain("manifest.json");
    expect(names.some((n) => n.startsWith("tables/"))).toBe(true);
  }
});
