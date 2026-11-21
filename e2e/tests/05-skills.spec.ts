// M2.2 and M3.12: the skill editor: make a skill, change it, see its versions, archive it,
// and restore it. Built-in skills are read-only.

import { marker } from "../lib/names.ts";
import { expect, test } from "../fixtures.ts";

function skillMd(name: string, body: string): string {
  return `---\nname: ${name}\ndescription: End-to-end test skill\n---\n\n# ${name}\n\n${body}\n`;
}

test("make, edit, archive, and restore a skill", async ({ ownerPage: page }) => {
  const name = `e2e-${marker("s").toLowerCase()}`;
  await page.goto("/skills");
  await expect(page.getByRole("region", { name: "code-review" })).toBeVisible();

  await page.getByRole("button", { name: "New skill" }).click();
  const form = page.getByRole("region", { name: "New skill" });
  await form.getByRole("textbox", { name: /^Name/ }).fill(name);
  await form.getByRole("textbox", { name: "SKILL.md", exact: true }).fill(skillMd(name, "First version."));
  await form.getByRole("button", { name: "Create" }).click();

  const edit = page.getByRole("form", { name: `Edit ${name}` });
  await expect(edit).toBeVisible();
  await edit.getByRole("textbox", { name: "SKILL.md", exact: true }).fill(skillMd(name, "Second version."));
  await edit.getByRole("textbox", { name: /^What changed/ }).fill("e2e edit");
  await edit.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("heading", { name: "Versions" })).toBeVisible();
  await expect(page.getByText("e2e edit")).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Archive" }).click();
  await page.goto("/skills");
  await expect(page.getByRole("region", { name })).toHaveCount(0);
  await page.getByText(/Archived skills \(\d+\)/).click();
  const archived = page.getByRole("list", { name: "Archived skills" });
  await archived.getByRole("listitem").filter({ hasText: name }).getByRole("button", { name: "Restore" }).click();
  await expect(page.getByRole("region", { name })).toBeVisible();
});

test("a built-in skill is read-only", async ({ ownerPage: page }) => {
  await page.goto("/skills");
  const card = page.getByRole("region", { name: "code-review" });
  await card.getByRole("button", { name: /^Open/ }).click();
  await expect(page.getByText("A built-in skill is read-only.", { exact: false })).toBeVisible();
});
