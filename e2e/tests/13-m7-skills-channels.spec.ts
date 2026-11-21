// M7.7: the builtin skill library and a skill command in the web app. M7.8: the setup API of
// the WhatsApp and email channels. The channel tests use fake credentials: a real number or
// SendGrid account is not part of the suite, so they check the refusals and the guards.

import { randomUUID } from "node:crypto";
import { readdirSync } from "node:fs";
import { marker } from "../lib/names.ts";
import { calledTool, sessionToolUses } from "../lib/trace.ts";
import { send, startSession, waitForReply } from "../lib/ui.ts";
import { expect, test } from "../fixtures.ts";

/** The builtin skills in this checkout. Staging can run an older commit, so it can have fewer. */
const REPO_BUILTINS = readdirSync(new URL("../../skills/", import.meta.url), { withFileTypes: true })
  .filter((entry) => entry.isDirectory()).length;
/** The library of M7.7 has 68 sama-written skills. */
const MIN_BUILTINS = 68;

interface Catalog {
  skills: { name: string; scope: string }[];
}

test("the builtin skill library has every skill", async ({ ownerApi, ownerPage: page, world }) => {
  const catalog = await ownerApi.get<Catalog>(`/workspaces/${world.org.workspaceId}/skills`);
  const builtins = catalog.skills.filter((s) => s.scope === "builtin");
  expect(builtins.length).toBeGreaterThanOrEqual(MIN_BUILTINS);
  expect(builtins.length).toBeLessThanOrEqual(REPO_BUILTINS);
  expect(builtins.map((s) => s.name)).toContain("decision-memo");

  await page.goto("/skills");
  await expect(page.getByRole("region", { name: "decision-memo" })).toBeVisible();
});

test("/decision-memo loads the skill and answers", async ({ ownerPage: page, ownerApi }) => {
  const word = marker("memo");
  const sessionId = await startSession(page, `e2e skill ${word}`);
  await send(
    page,
    `/decision-memo Choose between Postgres and SQLite for a small internal tool. ` +
      `Keep it under 150 words and end with the word ${word}.`,
  );
  await waitForReply(page, word);
  const uses = await sessionToolUses(ownerApi, sessionId);
  const loaded = uses.filter((u) => calledTool(u, /skill_view$/) && u.ok);
  expect(JSON.stringify(loaded.map((u) => u.arguments))).toContain("decision-memo");
});

const FAKE_WHATSAPP = [
  {
    provider: "meta",
    number: "+15550000001",
    api_token: "e2e-fake-token",
    app_secret: "e2e-fake-secret",
    phone_number_id: "100000000000001",
  },
  {
    provider: "twilio",
    number: "+15550000002",
    api_token: "e2efakeauthtoken0000000000000000",
    account_sid: `AC${"0".repeat(32)}`,
  },
  {
    provider: "gupshup",
    number: "+15550000003",
    api_token: "e2e-fake-key",
    app_id: "e2e-fake-app",
    app_name: "e2efakeapp",
  },
];

test("WhatsApp and email refuse credentials that the provider does not accept", async ({
  ownerApi,
  world,
}) => {
  const base = `/orgs/${world.org.id}/integrations`;
  for (const body of FAKE_WHATSAPP) {
    const answer = await ownerApi.call<{ detail?: string }>(`${base}/whatsapp`, {
      method: "POST",
      body,
      allow: [400, 401, 403, 409, 422, 502, 503],
    });
    expect(answer.status, `${body.provider}: ${JSON.stringify(answer.data)}`).toBeGreaterThanOrEqual(400);
    expect(JSON.stringify(answer.data)).not.toContain(body.api_token);
  }
  const email = await ownerApi.call(`${base}/email`, {
    method: "POST",
    body: {
      address: "agent@inbound.example.com",
      api_key: "SG.e2e-fake-key",
      public_key: "not a public key, but long enough",
    },
    allow: [400, 401, 403, 409, 422, 502, 503],
  });
  expect(email.status).toBeGreaterThanOrEqual(400);

  // No row stays after a refused connect.
  const rows = await ownerApi.get<{ kind: string }[]>(base);
  expect(rows.map((r) => r.kind)).not.toContain("whatsapp");
  expect(rows.map((r) => r.kind)).not.toContain("email");
});

test("the channel routes refuse what they must", async ({ ownerApi, memberApi, world, request }) => {
  // A WhatsApp or email chat is private, so a group code is refused. Without a connected
  // number or address, a user code is refused too.
  for (const channel of ["whatsapp", "email"]) {
    const group = await ownerApi.call(`/channels/${channel}/pairing-codes`, {
      method: "POST",
      body: { workspace_id: world.org.workspaceId, agent_profile_id: world.org.profileId, kind: "group" },
      allow: [400],
    });
    expect(group.status).toBe(400);
    const user = await ownerApi.call(`/channels/${channel}/pairing-codes`, {
      method: "POST",
      body: { workspace_id: world.org.workspaceId, agent_profile_id: world.org.profileId },
      allow: [409],
    });
    expect(user.status).toBe(409);
  }

  // An unknown integration or a wrong token gets 401, without a hint which one is wrong.
  const hook = await request.post(`${world.apiUrl}/whatsapp/${randomUUID()}/webhook/${marker("t")}`, {
    data: { object: "whatsapp_business_account", entry: [] },
  });
  expect(hook.status()).toBe(401);
  const mail = await request.post(`${world.apiUrl}/email/${randomUUID()}/inbound`, {
    multipart: { from: "someone@example.com", text: "hello" },
  });
  expect(mail.status()).toBe(401);

  // Only an org admin connects a channel.
  const joined = (await memberApi.me()).orgs.some((o) => o.id === world.org.id);
  if (joined) {
    const denied = await memberApi.call(`/orgs/${world.org.id}/integrations/whatsapp`, {
      method: "POST",
      body: FAKE_WHATSAPP[1],
      allow: [403],
    });
    expect(denied.status).toBe(403);
  }
});

test("the Settings page opens the WhatsApp form from a link and shows a refusal", async ({
  ownerPage: page,
  ownerApi,
  world,
}) => {
  // The link that connection_request gives opens the form with the provider (Section 11.9).
  await page.goto("/settings?connect=whatsapp&provider=twilio");
  const form = page.getByRole("form", { name: "Connect WhatsApp" });
  await expect(form).toBeVisible();
  await expect(form.getByRole("combobox", { name: "Provider" })).toHaveValue("twilio");
  const twilio = FAKE_WHATSAPP[1]!;
  await form.getByRole("textbox", { name: /^Business number/ }).fill(twilio.number);
  await form.getByLabel(/^Auth token/).fill(twilio.api_token);
  await form.getByRole("textbox", { name: /^Account SID/ }).fill(twilio.account_sid!);
  const answer = page.waitForResponse(
    (r) => r.request().method() === "POST" && r.url().endsWith("/integrations/whatsapp"),
  );
  await form.getByRole("button", { name: "Connect", exact: true }).click();
  expect((await answer).status()).toBeGreaterThanOrEqual(400);
  const section = form.locator("xpath=..");
  await expect(section.locator(".alert-error")).toBeVisible();
  await expect(section.locator(".alert-error")).not.toContainText(twilio.api_token);

  // The email form is there too, for an admin.
  await page.goto("/settings?connect=email");
  const mail = page.getByRole("form", { name: "Connect Email" });
  await expect(mail.getByRole("textbox", { name: /^Inbound address/ })).toBeVisible();
  await expect(mail.getByRole("textbox", { name: /^Public key of the parse security policy/ })).toBeVisible();

  const rows = await ownerApi.get<{ kind: string }[]>(`/orgs/${world.org.id}/integrations`);
  expect(rows.map((r) => r.kind)).not.toContain("whatsapp");
});
