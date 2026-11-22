// M7.2: the coding agents Claude Code and Codex in the session sandbox. The direct mode
// (/claude until /sama), one task for Codex (/codex <task>), and the approval card of
// coding_agent_run when the sama agent calls the tool. Each request asks for an approval by
// default (Section 15.4).

import { marker } from "../lib/names.ts";
import { approvalCard, decide, send, startSession, waitForReply, waitIdle } from "../lib/ui.ts";
import { expect, test } from "../fixtures.ts";

/** A CLI start in a new sandbox, a model turn, and the diff can take some minutes. */
const CODING_TIMEOUT_MS = 8 * 60_000;

test("direct mode: /claude sends the messages to Claude Code until /sama", async ({ ownerPage: page }) => {
  test.setTimeout(15 * 60_000);
  const word = marker("claude");
  await startSession(page, `e2e claude ${word}`);
  await send(page, "/claude");
  await expect(page.getByText(/Direct mode: each message of this session now goes to Claude Code/)).toBeVisible();

  await send(page, `Create a file named e2e.txt that contains the word ${word}. Then print the file.`);
  await decide(page, "coding_agent_run", "Approve", CODING_TIMEOUT_MS);
  await waitForReply(page, word, CODING_TIMEOUT_MS);
  await waitIdle(page, CODING_TIMEOUT_MS);

  await send(page, "/sama");
  await expect(page.getByText("sama answers the messages of this session again.")).toBeVisible();
  const back = marker("sama");
  await send(page, `Reply with the word ${back} and nothing else.`);
  await waitForReply(page, back);
  // The sama runtime answers without a coding agent request.
  await expect(approvalCard(page, "coding_agent_run")).toHaveCount(0);
});

test("/codex <task> sends one task to Codex", async ({ ownerPage: page }) => {
  test.setTimeout(15 * 60_000);
  const word = marker("codex");
  await startSession(page, `e2e codex ${word}`);
  await send(page, `/codex Create a file named hello.txt that contains the word ${word}. Then print the file.`);
  await decide(page, "coding_agent_run", "Approve", CODING_TIMEOUT_MS);
  await waitForReply(page, word, CODING_TIMEOUT_MS);
  await waitIdle(page, CODING_TIMEOUT_MS);
});

test("the agent's coding_agent_run call asks for an approval", async ({ ownerPage: page }) => {
  await startSession(page, `e2e coding tool ${marker("tool")}`);
  await send(
    page,
    "Use the coding_agent_run tool with the agent claude_code and the prompt: list the files " +
      "of the work folder. Then tell me what it found.",
  );
  const card = approvalCard(page, "coding_agent_run");
  await expect(card).toBeVisible({ timeout: 4 * 60_000 });
  await expect(card).toContainText("claude_code");
  await decide(page, "coding_agent_run", "Deny");
  await waitIdle(page);
});
