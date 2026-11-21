// M7.3: custom tools. On the Tools page an owner adds an HTTP request tool (a public echo API)
// and a Python script tool, runs the test button of each, and the agent calls each in a chat
// (through tool_search). A member's write to an org tool only proposes a version.

import type { Page } from "@playwright/test";
import { SamaApi } from "../lib/api.ts";
import { marker } from "../lib/names.ts";
import { calledTool, sessionToolUses } from "../lib/trace.ts";
import { send, startSession, waitForReplyApproving } from "../lib/ui.ts";
import { expect, test } from "../fixtures.ts";

const SCHEMA = JSON.stringify({
  type: "object",
  properties: { word: { type: "string", description: "The word to send." } },
  required: ["word"],
});

/** A public echo API: it answers the query in `args`. */
const ECHO_URL = "https://postman-echo.com/get?word={{word}}";
const SCRIPT = [
  "import json, sys",
  "args = json.load(sys.stdin)",
  'print(json.dumps({"upper": args["word"].upper()}))',
].join("\n");

const made: string[] = [];

/** Fills the common fields of the Add a tool form. */
async function startTool(page: Page, name: string, kind: "http" | "script", description: string) {
  await page.goto("/tools");
  await page.getByRole("button", { name: "Add a tool" }).click();
  const form = page.getByRole("form", { name: "Add a tool" });
  await form.getByRole("textbox", { name: /^Name/ }).fill(name);
  await form.getByRole("combobox", { name: /^Kind/ }).selectOption(kind);
  await form.getByRole("combobox", { name: /^Who can use it/ }).selectOption("user");
  await form.getByRole("textbox", { name: /^Description/ }).fill(description);
  await form.getByRole("combobox", { name: /^Policy/ }).selectOption("allow");
  await form.getByRole("textbox", { name: /^Arguments \(JSON schema\)/ }).fill(SCHEMA);
  return form;
}

/** Saves the form and returns the id of the new tool from the API answer. */
async function save(page: Page, form: ReturnType<Page["getByRole"]>, workspaceId: string): Promise<string> {
  const created = page.waitForResponse(
    (r) => r.request().method() === "POST" && r.url().endsWith(`/workspaces/${workspaceId}/custom-tools`),
  );
  await form.getByRole("button", { name: "Save", exact: true }).click();
  const response = await created;
  expect(response.status(), await response.text()).toBeLessThan(300);
  const tool = (await response.json()) as { id: string; current_version: number | null };
  expect(tool.current_version).toBe(1);
  made.push(tool.id);
  return tool.id;
}

/** Runs the test button of a tool and checks the answer. */
async function runTest(page: Page, name: string, args: object, expected: string) {
  const panel = page.getByRole("form", { name: `Test ${name}` });
  await panel.getByRole("textbox", { name: /^Arguments \(JSON\)/ }).fill(JSON.stringify(args));
  await panel.getByRole("button", { name: "Run the test" }).click();
  const result = panel.getByRole("status", { name: "Test result" });
  await expect(result).toContainText("It worked", { timeout: 120_000 });
  await expect(result).toContainText(expected);
}

/** A chat where the agent finds the tool and calls it; the trace must show the call. */
async function chatCalls(page: Page, api: SamaApi, toolName: string, word: string, expected: string) {
  const sessionId = await startSession(page, `e2e tool ${toolName}`);
  await send(
    page,
    `Find the tool ${toolName} with tool_search and call it with the word ${word}. ` +
      "Then reply with exactly what it returned.",
  );
  await waitForReplyApproving(page, expected);
  const uses = await sessionToolUses(api, sessionId);
  expect(uses.some((u) => calledTool(u, new RegExp(`${toolName}$`)) && u.ok)).toBe(true);
}

test("an HTTP request tool: add, test, and use in a chat", async ({ ownerPage: page, ownerApi, world }) => {
  const name = `e2e_http_${marker("h").toLowerCase()}`;
  const form = await startTool(page, name, "http", "Sends a word to an echo API and returns it.");
  await form.getByRole("combobox", { name: /^Method/ }).selectOption("GET");
  await form.getByRole("textbox", { name: /^URL/ }).fill(ECHO_URL);
  await form.getByRole("combobox", { name: /^Auth/ }).selectOption("none");
  await form.getByRole("textbox", { name: /^Answer path/ }).fill("args");
  await save(page, form, world.org.workspaceId);

  const word = marker("echo");
  await runTest(page, name, { word }, word);
  const other = marker("call");
  await chatCalls(page, ownerApi, `custom_${name}`, other, other);
});

test("a Python script tool: add, test, and use in a chat", async ({ ownerPage: page, ownerApi, world }) => {
  test.setTimeout(10 * 60_000);
  const name = `e2e_script_${marker("s").toLowerCase()}`;
  const form = await startTool(page, name, "script", "Returns the word in capital letters.");
  await form.getByRole("combobox", { name: /^Language/ }).selectOption("python");
  await form.getByRole("textbox", { name: /^Code/ }).fill(SCRIPT);
  await save(page, form, world.org.workspaceId);

  // The script changes the word, so the answer proves that it ran.
  const word = marker("up");
  await runTest(page, name, { word: word.toLowerCase() }, word);
  const other = marker("ch");
  await chatCalls(page, ownerApi, `custom_${name}`, other.toLowerCase(), other);
});

test("a member's org tool waits for an admin", async ({ memberApi, world }) => {
  const joined = (await memberApi.me()).orgs.some((o) => o.id === world.org.id);
  test.skip(!joined, "The member joins the org in 06-members.");
  const answer = await memberApi.call<{ id?: string; current_version?: number | null; pending?: number }>(
    `/workspaces/${world.org.workspaceId}/custom-tools`,
    {
      method: "POST",
      body: {
        name: `e2e_member_${marker("m").toLowerCase()}`,
        scope: "org",
        kind: "script",
        description: "A proposal of a member.",
        input_schema: { type: "object", properties: {} },
        policy: "allow",
        script: { language: "python", code: "print(1)", timeout_s: 30 },
      },
      allow: [403],
    },
  );
  if (answer.status === 403) return;
  // The write is a proposal: no version runs until an admin approves it.
  expect(answer.data.current_version ?? null).toBeNull();
  expect(answer.data.pending ?? 0).toBeGreaterThan(0);
  if (answer.data.id) made.push(answer.data.id);
});

test.afterAll(async ({ world, tokenCache }) => {
  const api = new SamaApi(world.apiUrl, async () => (await tokenCache.forUser("owner")).access_token);
  for (const id of made) await api.delete(`/custom-tools/${id}`, [403, 404]);
});
