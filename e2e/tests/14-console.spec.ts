// M7.10: the settings console. Each of its 14 sections opens (API keys since PR 90). A webhook goes through create, a signed
// test, an edit, and a delete. The harness changes the reasoning effort of the agent.

import { expect, test } from "../fixtures.ts";
import { marker } from "../lib/names.ts";

const SECTIONS: [string, string][] = [
  ["setup", "Setup"],
  ["slack", "Slack"],
  ["roles", "Roles"],
  ["api-keys", "API keys"],
  ["harness", "Harness"],
  ["tools", "Tools"],
  ["subagents", "Sub-agents"],
  ["skills", "Skills"],
  ["mcp", "MCP servers"],
  ["webhooks", "Webhooks"],
  ["triggers", "Triggers"],
  ["schedules", "Schedules"],
  ["workflows", "Workflows"],
  ["sessions", "Sessions"],
];

test("each console section opens", async ({ ownerPage: page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/console/setup");
  const nav = page.getByRole("navigation", { name: "Console sections" });
  await expect(nav.getByRole("link")).toHaveCount(SECTIONS.length);
  for (const [id, title] of SECTIONS) {
    await nav.getByRole("link", { name: title, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/console/${id}$`));
    await expect(page.getByRole("heading", { level: 1, name: title, exact: true })).toBeVisible();
    await expect(page.locator(".alert-error")).toHaveCount(0);
  }
  expect(errors).toEqual([]);
});

test("make a webhook, send a signed test, edit it, and delete it", async ({
  ownerPage: page,
  ownerApi,
  world,
}) => {
  const name = `e2e-console-${marker("w").toLowerCase()}`;
  const triggers = `/workspaces/${world.org.workspaceId}/triggers`;
  await page.goto("/console/webhooks");
  await page.getByRole("button", { name: "New webhook" }).first().click();
  const form = page.getByRole("form", { name: "New webhook" });
  await form.getByRole("textbox", { name: /^Name/ }).fill(name);
  await form.getByRole("radio", { name: /A run of an agent/ }).check();
  await form.getByRole("textbox", { name: /^Prompt/ }).fill("Reply with the word OK.");
  await form.getByRole("button", { name: "Create the webhook" }).click();

  try {
    // The secret shows once.
    const secret = page.getByRole("alert", { name: "New secret" });
    await expect(secret).toBeVisible();
    await expect(secret.getByLabel("Signing secret")).toHaveText(/^whsec_/);
    await secret.getByRole("button", { name: "I copied the secret" }).click();
    await expect(page.getByRole("heading", { level: 2, name })).toBeVisible();

    // A signed test that starts nothing.
    const test = page.getByRole("form", { name: "Send a test" });
    await test.getByRole("button", { name: "Send the test" }).click();
    await expect(test.locator(".alert-success")).toBeVisible();

    // Edit the prompt.
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    const edit = page.getByRole("form", { name: `Edit ${name}` });
    await edit.getByRole("textbox", { name: /^Prompt/ }).fill("Reply with the word EDITED.");
    await edit.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByText("Reply with the word EDITED.")).toBeVisible();
    // The save is a request after the click, so read the API until it has the new prompt.
    await expect
      .poll(async () => {
        const rows = await ownerApi.get<{ name: string; prompt: string }[]>(triggers);
        return rows.find((t) => t.name === name)?.prompt;
      })
      .toBe("Reply with the word EDITED.");

    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Delete", exact: true }).click();
    await expect(page.getByRole("button", { name: `Open ${name}` })).toHaveCount(0);
  } finally {
    // Remove it with the API when a step above failed.
    const rows = await ownerApi.get<{ id: string; name: string }[]>(triggers);
    for (const row of rows.filter((t) => t.name === name)) {
      await ownerApi.delete(`${triggers}/${row.id}`);
    }
  }
  const after = await ownerApi.get<{ name: string }[]>(triggers);
  expect(after.map((t) => t.name)).not.toContain(name);
});

test("the harness sets the reasoning effort of the agent", async ({ ownerPage: page, ownerApi, world }) => {
  const path = `/orgs/${world.org.id}/agent-profiles/${world.org.profileId}`;
  const before = await ownerApi.get<{ reasoning_effort: string | null }>(path);
  const wanted = before.reasoning_effort === "low" ? "high" : "low";
  try {
    await page.goto("/console/harness");
    const form = page.getByRole("form", { name: /^Harness of / });
    await form.getByRole("combobox", { name: /^Reasoning effort/ }).selectOption(wanted);
    await form.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByText("Saved. The change applies to new sessions.")).toBeVisible();
    const after = await ownerApi.get<{ reasoning_effort: string | null }>(path);
    expect(after.reasoning_effort).toBe(wanted);
  } finally {
    await ownerApi.patch(path, { reasoning_effort: before.reasoning_effort });
  }
});
