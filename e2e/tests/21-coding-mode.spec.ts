// M7.9: coding mode. A public repository is attached on the Code page, /plan asks for a plan,
// the plan is approved, and the agent does the work. The Code page shows the changes as diffs
// and the commands, and Undo restores the files.

import { poll } from "../lib/api.ts";
import { marker } from "../lib/names.ts";
import { approveUntilIdle, send, startSession } from "../lib/ui.ts";
import { expect, test } from "../fixtures.ts";

test.describe.configure({ mode: "serial" });

/** A tiny public repository. It clones without credentials, read-only. */
const REPO = "https://github.com/octocat/Hello-World";

interface Coding {
  plan_mode: boolean;
  repos: { repo: string; status: string; folder?: string }[];
  plan: { id: string; status: string; body?: string } | null;
}

interface Diff {
  files: { path: string; status: string }[];
}

test("attach a repo, plan, approve, see the changes and the commands, and undo", async ({
  ownerPage: page,
  ownerApi,
}) => {
  test.setTimeout(20 * 60_000);
  const word = marker("code");
  const sessionId = await startSession(page, `e2e coding ${word}`);

  // Attach the repository on the Code page.
  await page.goto(`/s/${sessionId}/code`);
  const tabs = page.getByRole("tablist", { name: "Code sections" });
  await tabs.getByRole("tab", { name: "Repos" }).click();
  const attach = page.getByRole("form", { name: "Attach a repo" });
  await attach.getByRole("textbox", { name: /^Repo$/ }).fill(REPO);
  await attach.getByRole("button", { name: "Attach", exact: true }).click();
  await expect(page.getByRole("list", { name: "Attached repos" })).toContainText(/Hello-World/);

  // Plan mode: the agent explores and proposes a plan, and changes no file.
  await page.goto(`/s/${sessionId}`);
  await send(
    page,
    `/plan In the Hello-World folder, create a file e2e.txt that contains the word ${word}, and a ` +
      "script check_e2e.sh that exits 0 only when e2e.txt has that word. Then run the script as the test.",
  );
  const proposed = await poll(
    "a proposed plan",
    async () => {
      const coding = await ownerApi.get<Coding>(`/sessions/${sessionId}/coding`);
      return coding.plan?.status === "proposed" ? coding : undefined;
    },
    { timeoutMs: 8 * 60_000, intervalMs: 5_000 },
  );
  expect(proposed.plan_mode).toBe(true);
  expect(proposed.repos[0]?.status).toBe("ready");

  // Approve the plan on the Code page; the agent then does the work in a new run.
  await page.goto(`/s/${sessionId}/code`);
  await tabs.getByRole("tab", { name: "Plan" }).click();
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(page.getByText("Plan approved. The agent starts the work.")).toBeVisible();
  await page.goto(`/s/${sessionId}`);
  await approveUntilIdle(page);
  const coding = await ownerApi.get<Coding>(`/sessions/${sessionId}/coding`);
  expect(coding.plan_mode).toBe(false);
  expect(coding.plan?.status).toBe("approved");

  // The Code page shows the new file as a diff and the script as a command.
  await page.goto(`/s/${sessionId}/code`);
  await tabs.getByRole("tab", { name: "Changes" }).click();
  await expect(page.getByRole("list", { name: "Changed files" })).toContainText("e2e.txt", { timeout: 60_000 });
  await tabs.getByRole("tab", { name: "Commands and tests" }).click();
  await expect(page.getByRole("list", { name: /^(Commands|Test runs)$/ }).first()).toContainText("check_e2e");

  // Undo restores the files from before the last run.
  await tabs.getByRole("tab", { name: "Checkpoints" }).click();
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await page.getByRole("button", { name: "Yes, restore" }).click();
  await poll(
    "e2e.txt to leave the diff after the undo",
    async () => {
      const diff = await ownerApi.get<Diff>(`/sessions/${sessionId}/diff?base=oldest`);
      return diff.files.some((f) => f.path.endsWith("e2e.txt")) ? undefined : diff;
    },
    { timeoutMs: 120_000, intervalMs: 5_000 },
  );
});
