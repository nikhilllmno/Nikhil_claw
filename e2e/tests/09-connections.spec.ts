// M3.4, M3.5, and M4.11: a connection from the Connections page. The test adds the MCP
// "everything" server as a hosted stdio connection, and the agent calls its echo tool.

import { marker } from "../lib/names.ts";
import { send, startSession, waitForReplyApproving } from "../lib/ui.ts";
import { expect, test } from "../fixtures.ts";

/** A pinned version: sama accepts only an exact version (Section 10.5). */
const EVERYTHING = { name: "@modelcontextprotocol/server-everything", version: "2.3.1" };

test("add the MCP everything server, use its echo tool, and remove it", async ({
  ownerPage: page,
}) => {
  test.setTimeout(12 * 60_000);
  const suffix = marker("m").toLowerCase();
  const name = `everything${suffix}`.slice(0, 20);
  const title = `E2E everything ${suffix}`;
  await page.goto("/connections");
  await page.getByRole("button", { name: "Add a stdio MCP server" }).click();
  const form = page.getByRole("region", { name: "Add a stdio MCP server" });
  await form.getByRole("textbox", { name: /^Name/ }).fill(name);
  await form.getByRole("textbox", { name: /^Title/ }).fill(title);
  await form.getByRole("textbox", { name: /^Package/ }).fill(EVERYTHING.name);
  await form.getByRole("textbox", { name: /^Version/ }).fill(EVERYTHING.version);
  await form.getByRole("textbox", { name: /^Arguments/ }).fill("stdio");
  await form.getByRole("button", { name: "Add and connect" }).click();

  const card = page.getByRole("region", { name: title });
  // The first start installs the package in a connector host. It can take minutes.
  await expect(card.getByText(/connected/i).first()).toBeVisible({ timeout: 6 * 60_000 });

  try {
    const word = marker("echo");
    await startSession(page, `e2e mcp ${word}`);
    await send(
      page,
      `Find the echo tool of the ${title} connection and call it with the message ${word}. ` +
        `Then reply with what it returned.`,
    );
    await waitForReplyApproving(page, word);
  } finally {
    await page.goto("/connections");
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("region", { name: title }).getByRole("button", { name: "Remove" }).click();
    await expect(page.getByRole("region", { name: title })).toHaveCount(0);
  }
});
