// M7.4: the sub-agent library. An owner makes a personal sub-agent in the console, runs its test
// run, adds it to the agent on the agent page, and the agent hands a task to it with
// delegate_task(agent=...). A member cannot write an org sub-agent.

import { SamaApi } from "../lib/api.ts";
import { marker } from "../lib/names.ts";
import { calledTool, sessionToolUses } from "../lib/trace.ts";
import { send, startSession, waitForReply, waitForReplyApproving } from "../lib/ui.ts";
import { expect, test } from "../fixtures.ts";

test.describe.configure({ mode: "serial" });

interface LibraryAgent {
  id: string;
  name: string;
  scope: string;
  version: number;
}

const name = `e2e-helper-${marker("h").toLowerCase()}`;
let agentId = "";

test("make a personal sub-agent in the console and run its test", async ({ ownerPage: page, ownerApi, world }) => {
  await page.goto("/console/subagents");
  // The section has the sub-agents of the agent, then the library (M7.4). The console names the
  // button of the library "New library sub-agent" (PR 90).
  await expect(page.getByRole("region", { name: /^Sub-agents of / })).toBeVisible();
  await page.getByRole("button", { name: "New library sub-agent", exact: true }).click();
  const form = page.getByRole("form", { name: "New sub-agent" });
  await form.getByRole("textbox", { name: /^Name/ }).fill(name);
  await form.getByRole("textbox", { name: /^Description/ }).fill("Answers short questions for the e2e test.");
  await form.getByRole("textbox", { name: /^Prompt/ }).fill("You answer in one short line. Follow the request exactly.");
  await form.getByRole("combobox", { name: /^Who can use it/ }).selectOption("user");
  const created = page.waitForResponse(
    (r) => r.request().method() === "POST" && new URL(r.url()).pathname === `/orgs/${world.org.id}/subagents`,
  );
  await form.getByRole("button", { name: "Create", exact: true }).click();
  const response = await created;
  expect(response.status()).toBeLessThan(300);
  const agent = (await response.json()) as LibraryAgent;
  agentId = agent.id;
  expect(agent.scope).toBe("user");

  const word = marker("test");
  const run = page.getByRole("form", { name: `Test ${name}` });
  await run.getByRole("textbox", { name: /^Message/ }).fill(`Reply with the word ${word} and nothing else.`);
  await run.getByRole("button", { name: "Start test run" }).click();
  await page.waitForURL(/\/s\/[0-9a-f-]{36}$/);
  await waitForReply(page, word);

  const list = await ownerApi.get<{ agents: LibraryAgent[] }>(
    `/orgs/${world.org.id}/subagents?workspace_id=${world.org.workspaceId}`,
  );
  expect(list.agents.map((a) => a.name)).toContain(name);
});

test("add the sub-agent to the agent and delegate a task to it", async ({ ownerPage: page, ownerApi, world }) => {
  test.skip(!agentId, "The create test did not run.");
  const path = `/orgs/${world.org.id}/agent-profiles/${world.org.profileId}`;
  const before = await ownerApi.get<{ library_subagents: string[] }>(path);
  try {
    await page.goto(`/agents/${world.org.profileId}`);
    const library = page.getByRole("combobox", { name: /^Add from the library/ });
    const option = await library.locator("option").filter({ hasText: name }).getAttribute("value");
    await library.selectOption(option!);
    await page.getByRole("button", { name: "Add", exact: true }).click();
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByText("Saved.")).toBeVisible();
    const after = await ownerApi.get<{ library_subagents: string[] }>(path);
    expect(after.library_subagents).toContain(agentId);

    const word = marker("delegate");
    const sessionId = await startSession(page, `e2e delegate ${word}`);
    await send(
      page,
      `Use the delegate_task tool with agent ${name} and the task: reply with the word ${word}. ` +
        "Then tell me the word that it gave.",
    );
    await waitForReplyApproving(page, word);
    const uses = await sessionToolUses(ownerApi, sessionId);
    const delegated = uses.filter((u) => calledTool(u, /delegate_task$/) && u.ok);
    expect(JSON.stringify(delegated.map((u) => u.arguments))).toContain(name);
  } finally {
    await ownerApi.patch(path, { library_subagents: before.library_subagents });
  }
});

test("a member cannot write an org sub-agent", async ({ memberApi, world }) => {
  const joined = (await memberApi.me()).orgs.some((o) => o.id === world.org.id);
  test.skip(!joined, "The member joins the org in 06-members.");
  const catalog = await memberApi.get<{ writable_scopes: string[] }>(
    `/orgs/${world.org.id}/subagents?workspace_id=${world.org.workspaceId}`,
  );
  expect(catalog.writable_scopes).not.toContain("org");
  const denied = await memberApi.call(`/orgs/${world.org.id}/subagents`, {
    method: "POST",
    body: {
      scope: "org",
      name: `e2e-member-${marker("m").toLowerCase()}`,
      description: "Not allowed.",
      prompt: "Not allowed.",
      toolsets: [],
      model: null,
      max_turns: null,
      max_cost_usd: null,
    },
    allow: [403],
  });
  expect(denied.status).toBe(403);
});

test.afterAll(async ({ world, tokenCache }) => {
  if (!agentId) return;
  const api = new SamaApi(world.apiUrl, async () => (await tokenCache.forUser("owner")).access_token);
  await api.delete(`/orgs/${world.org.id}/subagents/${agentId}`, [404, 409]);
});
