// M1.13 and M3.2: approval cards in the web app. M3.9: steer and a second request while a run
// is busy. The busy rule is that both requests get an answer (roadmap M7.1).

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

test("a steer note and a second request during a busy run both get an answer", async ({
  ownerPage: page,
}) => {
  const first = marker("first");
  const steer = marker("steer");
  const second = marker("second");
  await startSession(page, `e2e steer ${first}`);
  await send(
    page,
    `Use the session_search tool three times, one call at a time, with the queries alpha, ` +
      `beta, and gamma. Then reply with the word ${first}.`,
  );
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeVisible();
  await send(page, `/steer Also put the word ${steer} at the end of your reply.`);
  await send(page, `Reply with the word ${second}.`);
  // Each request gets an answer: the steer note in the first answer or in its own answer,
  // and the second request after the first run or inside it.
  await waitForReply(page, first);
  await waitForReply(page, steer);
  await waitForReply(page, second);
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
