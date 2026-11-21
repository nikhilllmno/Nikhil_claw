// M7.5: Hermes tool parity. The tools moa, x_search, and video_analyze in a chat (each checked
// in the trace); a cheaper model for delegated tasks and the Mem0 memory provider on the agent
// page; and the settings of a skill for the workspace.

import type { Page } from "@playwright/test";
import type { SamaApi } from "../lib/api.ts";
import { marker } from "../lib/names.ts";
import { calledTool, sessionToolUses, type ToolUse } from "../lib/trace.ts";
import { send, startSession, waitForReply, waitForReplyApproving, waitIdle } from "../lib/ui.ts";
import { expect, test } from "../fixtures.ts";

/** A 19-second public video ("Me at the zoo"). Gemini reads a YouTube link directly. */
const VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw";

async function chatWithTool(
  page: Page,
  api: SamaApi,
  prompt: string,
  answer: string | RegExp,
  tool: RegExp,
): Promise<ToolUse[]> {
  const sessionId = await startSession(page, `e2e parity ${marker("p")}`);
  await send(page, prompt);
  await waitForReplyApproving(page, answer);
  await waitIdle(page);
  const uses = (await sessionToolUses(api, sessionId)).filter((u) => calledTool(u, tool));
  expect(uses.length, `no call of ${tool}`).toBeGreaterThan(0);
  return uses;
}

test("moa answers with a mixture of models", async ({ ownerPage: page, ownerApi }) => {
  const word = marker("moa");
  const uses = await chatWithTool(
    page,
    ownerApi,
    `Use the moa tool with the question: What is the capital of France? Then reply with its answer and the word ${word}.`,
    word,
    /moa$/,
  );
  expect(uses.some((u) => u.ok)).toBe(true);
});

test("x_search searches recent posts on X", async ({ ownerPage: page, ownerApi }) => {
  const word = marker("x");
  // The answer of X can be posts or a short note that the account has no credits; both
  // prove that the tool reached the X API.
  const uses = await chatWithTool(
    page,
    ownerApi,
    `Use the x_search tool with the query "OpenAI" and limit 10. Then tell me how many posts it found, and end with the word ${word}.`,
    word,
    /x_search$/,
  );
  expect(uses[0]!.output.length).toBeGreaterThan(0);
});

test("video_analyze reads a YouTube video", async ({ ownerPage: page, ownerApi }) => {
  const uses = await chatWithTool(
    page,
    ownerApi,
    `Use the video_analyze tool with the source ${VIDEO} and the question: which animals are in the video? Then reply with the answer.`,
    /elephant/i,
    /video_analyze$/,
  );
  expect(uses.some((u) => u.ok)).toBe(true);
});

test("a cheaper model for delegated tasks and the Mem0 memory", async ({ ownerPage: page, ownerApi, world }) => {
  test.setTimeout(12 * 60_000);
  const path = `/orgs/${world.org.id}/agent-profiles/${world.org.profileId}`;
  const before = await ownerApi.get<{ delegation_model: string | null; memory_provider: string }>(path);
  try {
    await page.goto(`/agents/${world.org.profileId}`);
    const delegation = page.getByRole("combobox", { name: /^Model for delegated tasks/ });
    const models = await delegation.locator("option").evaluateAll((nodes) =>
      nodes.map((n) => (n as HTMLOptionElement).value).filter(Boolean),
    );
    const cheap = models.find((m) => m.includes("haiku")) ?? models[0]!;
    await delegation.selectOption(cheap);
    await page.getByRole("combobox", { name: /^Long-term memory/ }).selectOption("mem0");
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByText("Saved.")).toBeVisible();
    const after = await ownerApi.get<{ delegation_model: string | null; memory_provider: string }>(path);
    expect(after).toMatchObject({ delegation_model: cheap, memory_provider: "mem0" });

    // A delegated child runs on the cheaper model.
    const word = marker("child");
    await chatWithTool(
      page,
      ownerApi,
      `Use the delegate_task tool with the task: reply with the word ${word}. Then tell me the word that it gave.`,
      word,
      /delegate_task$/,
    );

    // A private chat goes to Mem0 at the end of the run. Mem0 stores it in the background,
    // so a new session asks memory_recall a few times.
    const lucky = String(1000 + Math.floor(Math.random() * 8999));
    await startSession(page, `e2e mem0 ${marker("m")}`);
    await send(page, `Please note: my lucky number is ${lucky}. Reply with OK.`);
    await waitForReply(page, /OK/i);
    await waitIdle(page);
    const sessionId = await startSession(page, `e2e recall ${marker("r")}`);
    let found = false;
    for (let attempt = 0; attempt < 4 && !found; attempt += 1) {
      if (attempt > 0) await new Promise((resolve) => setTimeout(resolve, 30_000));
      await send(page, "Use the memory_recall tool to find my lucky number. Reply with the number, or NONE.");
      await waitIdle(page);
      found = (await page.getByRole("article", { name: "Assistant", exact: true }).allTextContents()).some((t) =>
        t.includes(lucky),
      );
    }
    const recalls = (await sessionToolUses(ownerApi, sessionId)).filter((u) => calledTool(u, /memory_recall$/));
    expect(recalls.some((u) => u.ok)).toBe(true);
    expect(found, "memory_recall did not return the number from Mem0 within 2 minutes").toBe(true);
  } finally {
    // An empty delegation model goes back to the model of the agent.
    await ownerApi.patch(path, {
      delegation_model: before.delegation_model ?? "",
      memory_provider: before.memory_provider,
    });
  }
});

test("a workspace admin sets the settings of a skill", async ({ ownerPage: page, ownerApi, world }) => {
  const name = `e2e-settings-${marker("k").toLowerCase()}`;
  const skillMd =
    `---\nname: ${name}\ndescription: A skill with a setting for the e2e test\nconfig:\n` +
    "  - key: default_city\n    description: The city when the user names none\n    default: London\n---\n\n" +
    `# ${name}\n\nUse the default_city setting when the user names no city.\n`;
  await ownerApi.post(`/workspaces/${world.org.workspaceId}/skills`, {
    action: "create",
    name,
    scope: "workspace",
    content: skillMd,
  });
  try {
    await page.goto("/skills");
    await page.getByRole("region", { name }).getByRole("button", { name: /^Open/ }).click();
    const form = page.getByRole("form", { name: `Settings of ${name}` });
    const city = form.getByRole("textbox", { name: /^default_city/ });
    await expect(city).toHaveAttribute("placeholder", "London");
    await city.fill("Kolkata");
    await form.getByRole("button", { name: "Save", exact: true }).click();
    const settings = await ownerApi.get<{ effective: Record<string, unknown> }>(
      `/workspaces/${world.org.workspaceId}/skills/${name}/settings`,
    );
    expect(settings.effective.default_city).toBe("Kolkata");
  } finally {
    await ownerApi.post(`/workspaces/${world.org.workspaceId}/skills`, { action: "archive", name, scope: "workspace" });
  }
});
