// M2.1 and M3.12: the memory editor, its history and revert, and an agent that reads the
// memory in a new session.

import type { Page } from "@playwright/test";
import { marker } from "../lib/names.ts";
import { send, startSession, waitForReply } from "../lib/ui.ts";
import { expect, test } from "../fixtures.ts";

/** Adds one entry to the user profile on the Memory page and waits for the save. */
async function addProfileEntry(page: Page, text: string): Promise<void> {
  await page.goto("/memory");
  const profile = page.getByRole("region", { name: "User profile" });
  await profile.getByRole("button", { name: "Edit" }).click();
  const form = page.getByRole("form", { name: "Edit User profile" });
  await form.getByRole("button", { name: "Add entry" }).click();
  await form.getByRole("textbox", { name: /^Entry \d+/ }).last().fill(text);
  const saved = page.waitForResponse(
    (r) => r.request().method() === "PUT" && r.url().endsWith("/memory/user"),
  );
  await form.getByRole("button", { name: "Save", exact: true }).click();
  expect((await saved).status()).toBe(200);
  await expect(form).toHaveCount(0);
  await expect(profile.getByText(text)).toBeVisible();
}

test("edit the user profile, use it in a chat, and revert it", async ({ ownerPage: page }) => {
  const word = marker("color");
  const entry = `My favorite color is ${word}.`;
  await addProfileEntry(page, `I test sama end to end (${marker("base")}).`);
  await addProfileEntry(page, entry);

  // The memory is in the prompt of each new session.
  await startSession(page, `e2e memory ${word}`);
  await send(page, "What is my favorite color? Reply with the color only.");
  await waitForReply(page, word);

  await page.goto("/memory");
  const profile = page.getByRole("region", { name: "User profile" });
  await profile.getByRole("button", { name: "History" }).click();
  page.once("dialog", (dialog) => dialog.accept());
  // The newest revision is first and has no revert button: revert to the one before it.
  await profile.getByRole("button", { name: "Revert to this" }).first().click();
  await expect(page.getByText(/Reverted\. The notes are now revision \d+\./)).toBeVisible();
  await expect(profile.getByText(entry)).toHaveCount(0);
});
