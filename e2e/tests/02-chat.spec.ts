// M1 and M3.6: a chat in the web app gets a streamed answer, on the Claude runtime (default)
// and on the OpenAI runtime. Also export (M3.12), run traces, and session search (M2.5).

import { readFile } from "node:fs/promises";
import { marker } from "../lib/names.ts";
import { replies, send, startSession, waitForReply, waitIdle } from "../lib/ui.ts";
import { expect, test } from "../fixtures.ts";

test.describe.configure({ mode: "serial" });

const word = marker("chat");
let sessionId = "";

test("chat on the Claude runtime gets an answer", async ({ ownerPage: page }) => {
  sessionId = await startSession(page, `e2e chat ${word}`);
  await expect(page.getByRole("heading", { name: "No messages yet" })).toBeVisible();
  await send(page, `Reply with the word ${word} and nothing else.`);
  await expect(page.getByRole("article", { name: "You" }).filter({ hasText: word })).toBeVisible();
  await waitForReply(page, word);
  await waitIdle(page);
  // The answer stays after a reload: it is saved, not only streamed.
  await page.reload();
  await expect(replies(page).filter({ hasText: word })).toHaveCount(1);
});

test("the runs page shows the trace of the run", async ({ ownerPage: page }) => {
  await page.goto(`/s/${sessionId}`);
  await page.getByRole("link", { name: "Runs" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
  const run = page.getByRole("list", { name: "Runs" }).getByRole("region").first();
  await expect(run.getByText("completed")).toBeVisible();
  await run.getByRole("button", { name: "Show the trace" }).click();
  await expect(run.getByRole("heading", { name: "Model calls" })).toBeVisible();
  await expect(run.getByRole("columnheader", { name: "Model" })).toBeVisible();
});

test("export the session as Markdown and as JSON", async ({ ownerPage: page }) => {
  await page.goto(`/s/${sessionId}`);
  await waitForReply(page, word, 30_000);
  for (const [name, check] of [
    [/Export \.md/, (text: string) => expect(text).toContain(word)],
    [/Export \.json/, (text: string) => expect(JSON.stringify(JSON.parse(text))).toContain(word)],
  ] as const) {
    const download = page.waitForEvent("download");
    await page.getByRole("button", { name }).click();
    const file = await (await download).path();
    check(await readFile(file, "utf8"));
  }
});

test("session search finds the session by a word of the chat", async ({ ownerPage: page }) => {
  await page.goto("/search");
  await page.getByLabel("Words to find").fill(word);
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const results = page.getByRole("list", { name: "Search results" });
  await expect(results.getByRole("link", { name: new RegExp(`e2e chat ${word}`) })).toBeVisible();
  await results.getByRole("link", { name: new RegExp(`e2e chat ${word}`) }).click();
  await expect(page).toHaveURL(new RegExp(`/s/${sessionId}`));
});

test("chat on the OpenAI runtime gets an answer", async ({ ownerPage: page, ownerApi, world }) => {
  const path = `/orgs/${world.org.id}/agent-profiles/${world.org.profileId}`;
  const before = await ownerApi.get<{ runtime: string; model: string }>(path);
  try {
    // An admin switches the runtime on the agent page.
    await page.goto(`/agents/${world.org.profileId}`);
    await page.getByLabel("Runtime").selectOption("openai_agents_sdk");
    const model = page.getByLabel("Model");
    const options = await model.locator("option").evaluateAll((nodes) =>
      nodes.map((node) => (node as HTMLOptionElement).value),
    );
    expect(options.length).toBeGreaterThan(0);
    // The cheapest model of the runtime, when the list has it.
    await model.selectOption(options.find((value) => value.includes("luna")) ?? options[0]!);
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByText("Saved.")).toBeVisible();
    const after = await ownerApi.get<{ runtime: string }>(path);
    expect(after.runtime).toBe("openai_agents_sdk");

    const other = marker("openai");
    await startSession(page, `e2e openai ${other}`);
    await send(page, `Reply with the word ${other} and nothing else.`);
    await waitForReply(page, other);
  } finally {
    await ownerApi.patch(path, { runtime: before.runtime, model: before.model });
  }
});
