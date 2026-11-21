// M1.13 and M3.2: approval cards in the web app. M3.9: Stop. The busy modes of M7.1 (join,
// parallel runs, /stop NUMBER) are in 16-busy-modes.

import { marker } from "../lib/names.ts";
import { decide, send, startSession, waitForReply, waitIdle } from "../lib/ui.ts";
import { expect, test } from "../fixtures.ts";

test("approve a sandbox command, then deny one", async ({ ownerPage: page }) => {
  const word = marker("approve");
  await startSession(page, `e2e approvals ${word}`);
  await send(page, `Use the Bash tool to run exactly: echo ${word}. Then reply with its output.`);
  // Bash asks by default (src/sama/gate/decide.py).
  await decide(page, "Bash", "Approve");
  await waitForReply(page, word);
  await waitIdle(page);

  const denied = marker("deny");
  await send(
    page,
    `Use the Bash tool to run exactly: echo ${denied}. If you cannot, reply with the word REFUSED.`,
  );
  await decide(page, "Bash", "Deny");
  await waitForReply(page, /REFUSED|denied|not approved|could not|cannot/i);
  await waitIdle(page);
});

test("stop ends a busy run", async ({ ownerPage: page }) => {
  await startSession(page, `e2e stop ${marker("stop")}`);
  await send(page, "Write a 2000-word story about a lighthouse, slowly.");
  const stop = page.getByRole("button", { name: "Stop", exact: true });
  await expect(stop).toBeVisible();
  await stop.click();
  await waitIdle(page, 60_000);
});
