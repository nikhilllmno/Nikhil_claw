// M7.6: sama as a server. An org admin makes an API key in the web app (Admin, API keys). The
// official OpenAI SDK uses /v1, and the MCP SDK uses /mcp. A revoked key stops at once.

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import OpenAI from "openai";
import { marker } from "../lib/names.ts";
import { expect, test } from "../fixtures.ts";

test.describe.configure({ mode: "serial" });

let apiKey = "";
let keyName = "";

test("an admin makes an API key in the web app", async ({ ownerPage: page, world }) => {
  keyName = `e2e-key-${marker("k").toLowerCase()}`;
  await page.goto("/admin/api-keys");
  await expect(page.getByRole("heading", { level: 1, name: "API keys" })).toBeVisible();
  const form = page.getByRole("form", { name: "Make an API key" });
  await form.getByRole("textbox", { name: /^Name/ }).fill(keyName);
  await form.getByRole("combobox", { name: /^Workspace/ }).selectOption(world.org.workspaceId);
  await form.getByRole("checkbox", { name: /OpenAI-compatible chat/ }).check();
  await form.getByRole("checkbox", { name: /MCP server/ }).check();
  await form.getByRole("button", { name: "Make key" }).click();
  apiKey = await page.getByRole("textbox", { name: "The new API key" }).inputValue();
  expect(apiKey).toMatch(/^sama_sk_[A-Za-z0-9_-]{43}$/);
});

test("the OpenAI SDK chats through /v1 and keeps the session", async ({ ownerApi, world }) => {
  test.skip(!apiKey, "The key test did not run.");
  const client = new OpenAI({ apiKey, baseURL: `${world.apiUrl}/v1`, maxRetries: 0, timeout: 300_000 });
  const models = await client.models.list();
  const ids = models.data.map((m) => m.id);
  expect(ids.length).toBeGreaterThan(0);

  const word = marker("sdk");
  const { data, response } = await client.chat.completions
    .create({ model: ids[0]!, messages: [{ role: "user", content: `Reply with the word ${word} and nothing else.` }] })
    .withResponse();
  expect(data.choices[0]?.message.content ?? "").toContain(word);
  expect(data.usage?.total_tokens ?? 0).toBeGreaterThan(0);
  const sessionId = response.headers.get("x-sama-session-id");
  expect(sessionId).toBeTruthy();

  // The same session goes on, in a stream: the session has the earlier message.
  const stream = await client.chat.completions.create(
    {
      model: ids[0]!,
      stream: true,
      stream_options: { include_usage: true },
      messages: [{ role: "user", content: "Which word did I ask you for? Reply with that word only." }],
    },
    { headers: { "X-Sama-Session-Id": sessionId! } },
  );
  let text = "";
  for await (const chunk of stream) text += chunk.choices[0]?.delta?.content ?? "";
  expect(text).toContain(word);

  // The session is a private session of the key user, so the owner sees it.
  const sessions = await ownerApi.sessions();
  expect(sessions.map((s) => s.id)).toContain(sessionId);

  const bad = new OpenAI({ apiKey: "sama_sk_not-a-real-key", baseURL: `${world.apiUrl}/v1`, maxRetries: 0 });
  await expect(bad.models.list()).rejects.toMatchObject({ status: 401 });
});

test("an MCP client lists the tools and sends a message through /mcp", async ({ world }) => {
  test.skip(!apiKey, "The key test did not run.");
  const client = new Client({ name: "sama-e2e", version: "1.0.0" });
  const transport = new StreamableHTTPClientTransport(new URL(`${world.apiUrl}/mcp`), {
    requestInit: { headers: { Authorization: `Bearer ${apiKey}` } },
  });
  await client.connect(transport);
  try {
    const tools = (await client.listTools()).tools.map((t) => t.name);
    expect(tools).toEqual(expect.arrayContaining(["agents_list", "send_message", "run_get", "session_read"]));
    const agents = await client.callTool({ name: "agents_list", arguments: {} });
    expect(JSON.stringify(agents.content)).toContain("sama");

    const word = marker("mcp");
    let result = await client.callTool({
      name: "send_message",
      arguments: { text: `Reply with the word ${word} and nothing else.`, wait_s: 45 },
    });
    let body = JSON.stringify(result.content);
    // A slow run gives its run_id; ask again until it ends.
    for (let i = 0; i < 6 && !body.includes(word); i += 1) {
      const runId = /"run_id\\?"\s*:\s*\\?"([0-9a-f-]{36})/.exec(body)?.[1];
      if (!runId) break;
      result = await client.callTool({ name: "run_get", arguments: { run_id: runId, wait_s: 45 } });
      body = JSON.stringify(result.content);
    }
    expect(body).toContain(word);
  } finally {
    await client.close();
  }
});

test("a revoked key stops at once, and a member cannot make keys", async ({
  ownerPage: page,
  memberApi,
  world,
}) => {
  test.skip(!apiKey, "The key test did not run.");
  await page.goto("/admin/api-keys");
  const row = page.locator("div").filter({ hasText: keyName }).filter({ has: page.getByRole("button", { name: "Revoke" }) }).last();
  page.once("dialog", (dialog) => dialog.accept());
  const revoked = page.waitForResponse(
    (r) => r.request().method() === "DELETE" && /\/api-keys\/[0-9a-f-]{36}$/.test(r.url()),
  );
  await row.getByRole("button", { name: "Revoke", exact: true }).click();
  expect((await revoked).status()).toBe(204);
  const client = new OpenAI({ apiKey, baseURL: `${world.apiUrl}/v1`, maxRetries: 0 });
  await expect(client.models.list()).rejects.toMatchObject({ status: 401 });

  const joined = (await memberApi.me()).orgs.some((o) => o.id === world.org.id);
  if (joined) {
    const denied = await memberApi.call(`/orgs/${world.org.id}/api-keys`, {
      method: "POST",
      body: { name: "member-key", workspace_id: world.org.workspaceId, scopes: ["chat"] },
      allow: [403],
    });
    expect(denied.status).toBe(403);
  }
});
