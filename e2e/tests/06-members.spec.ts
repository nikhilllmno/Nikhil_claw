// M6.5: an admin makes an invite link on the Members page, and the invited user joins the org
// with the link. A member does not see the admin pages.

import { signInPage } from "../fixtures.ts";
import { expect, test } from "../fixtures.ts";

test.describe.configure({ mode: "serial" });

test("invite the member with a link and join", async ({
  ownerPage: page,
  browser,
  world,
  tokenCache,
}) => {
  const member = world.users.member!;
  await page.goto("/members");
  const invite = page.getByRole("region", { name: "Invite a person" });
  await invite.getByLabel("Email").fill(member.email);
  await invite.getByRole("combobox", { name: /^Role/ }).selectOption({ label: "Member" });
  await invite.getByRole("combobox", { name: /^Workspace/ }).selectOption({ index: 1 });
  await invite.getByRole("button", { name: "Make an invite link" }).click();
  const link = await page.getByRole("textbox", { name: "Invite link" }).inputValue();
  expect(link).toMatch(/\/invite#[A-Za-z0-9_-]{16,200}$/);
  await expect(page.getByRole("region", { name: "Open invites" })).toContainText(member.email);

  // The member opens the link in another browser.
  const context = await browser.newContext();
  try {
    const other = await context.newPage();
    await signInPage(other, await tokenCache.forUser("member"));
    await other.goto(new URL(link).pathname + new URL(link).hash);
    await other.getByRole("button", { name: "Join the org" }).click();
    await expect(other.getByRole("heading", { name: "What should we work on?" })).toBeVisible();

    // A member does not get the Billing link, and the Members page is read-only.
    await expect(other.getByRole("link", { name: /^Billing/ })).toHaveCount(0);
    await other.goto("/members");
    await expect(
      other.getByText("Only an owner or admin can invite people and change roles."),
    ).toBeVisible();
  } finally {
    await context.close();
  }

  await page.reload();
  await expect(page.getByRole("region", { name: world.org.name })).toContainText(/member/i);
});

test("the API keeps admin routes from a member", async ({ memberApi, world }) => {
  const billing = await memberApi.call(`/orgs/${world.org.id}/billing`, { allow: [403] });
  expect(billing.status).toBe(403);
  const exported = await memberApi.call(`/orgs/${world.org.id}/export`, { allow: [403] });
  expect(exported.status).toBe(403);
});
