// M3.8, M6.5, and M6.9: sign-up, sign-in, the first org, the setup guide, and sign-out.

import { randomBytes } from "node:crypto";
import type { NewOrgOut } from "../lib/api.ts";
import { marker, newPassword, orgName, testEmail } from "../lib/names.ts";
import { waitForReply } from "../lib/ui.ts";
import { recordExtra, type TestUser } from "../lib/world.ts";
import { expect, test } from "../fixtures.ts";

test.describe.configure({ mode: "serial" });

test("a wrong password shows an error and does not sign in", async ({ page, world }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill(world.users.owner!.email);
  await page.getByLabel("Password").fill("not-the-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator(".alert-error")).toBeVisible();
  await expect(page).toHaveURL(/\/login/);
});

test("sign up, confirm the email, sign in, make the first org, follow the setup guide, and sign out", async ({
  page,
  world,
  supabaseAdmin,
}, testInfo) => {
  // A retry is a new person: the user of the failed try is confirmed already, and the
  // teardown removes it with its org.
  const attempt = testInfo.retry === 0 ? "" : `r${testInfo.retry}`;
  const email = testEmail(world.runId, `signup${attempt}`);
  const password = newPassword((n) => randomBytes(n));
  let user: TestUser | null = null;

  // The staging project sends email only to its team, so the browser's sign-up request goes
  // to the admin API instead. It makes the same unconfirmed user and sends no email. The page
  // gets the answer that Supabase gives when the address needs a confirmation.
  await page.route("**/auth/v1/signup**", async (route) => {
    const body = route.request().postDataJSON() as Record<string, string>;
    expect(body.email).toBe(email);
    expect(body.code_challenge_method).toBe("s256");
    expect(body.code_challenge?.length ?? 0).toBeGreaterThan(20);
    const created = await supabaseAdmin.signUpWithoutEmail(body.email!, body.password!);
    user = { email, password, authUserId: created.id };
    recordExtra({ user });
    await route.fulfill({ json: { id: created.id, email, confirmation_sent_at: new Date() } });
  });

  await page.goto("/login");
  await page.getByRole("button", { name: "Create an account" }).click();
  await expect(page.getByText("Create an account.")).toBeVisible();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("Look in your email for the link that confirms your address.")).toBeVisible();
  expect(user).not.toBeNull();

  // The link in the email confirms the address.
  await supabaseAdmin.confirmEmail(user!.authUserId);

  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  // A new user has no org: the welcome page offers to make one.
  await expect(page.getByRole("heading", { name: "Welcome to sama" })).toBeVisible();
  const name = orgName(world.runId, `onboarding${attempt}`);
  await page.getByLabel("Org name").fill(name);
  const created = page.waitForResponse(
    (r) => r.request().method() === "POST" && new URL(r.url()).pathname === "/orgs",
  );
  await page.getByRole("button", { name: "Create the org" }).click();
  const response = await created;
  expect(response.status()).toBe(201);
  const org = (await response.json()) as NewOrgOut;
  // Record the org for the teardown before the next step can fail.
  recordExtra({
    org: {
      id: org.id,
      slug: org.slug,
      name,
      workspaceId: org.workspace_id,
      profileId: org.agent_profile_id,
      owner: email,
    },
  });
  expect(org.plan).toBe("free");

  // The setup guide (M6.9) opens after the first org: plan, chat app, agent, first message.
  await expect(page.getByRole("heading", { level: 1, name: `Set up ${name}` })).toBeVisible();
  await page.getByRole("button", { name: /^Keep the .* plan$/ }).click();
  await page.getByRole("button", { name: "Use only the web app for now" }).click();
  await page.getByRole("radio").first().check();
  await page.getByRole("button", { name: "Use this agent" }).click();
  const word = marker("first");
  await page.getByRole("textbox", { name: "Your message" }).fill(`Reply with the word ${word}.`);
  await page.getByRole("button", { name: "Send and open the chat" }).click();
  await page.waitForURL(/\/s\/[0-9a-f-]{36}$/);
  await waitForReply(page, word);

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
  // A page of the shell sends a signed-out user to the sign-in page.
  await page.goto("/memory");
  await expect(page).toHaveURL(/\/login/);
});
