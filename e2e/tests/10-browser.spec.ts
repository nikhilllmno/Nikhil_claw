// M5.1: the cloud browser tools on Browserbase. M5.6: the browser_task sub-agent.

import { marker } from "../lib/names.ts";
import { send, startSession, waitForReplyApproving } from "../lib/ui.ts";
import { expect, test } from "../fixtures.ts";

test("the agent opens a page in the cloud browser", async ({ ownerPage: page }) => {
  await startSession(page, `e2e browser ${marker("b")}`);
  await send(
    page,
    "Use the browser_open tool to open https://example.com in the cloud browser. " +
      "Then reply with the main heading of the page.",
  );
  await waitForReplyApproving(page, /Example Domain/i);
});

test("the browser_task sub-agent reaches a goal", async ({ ownerPage: page }) => {
  await startSession(page, `e2e browser task ${marker("t")}`);
  await send(
    page,
    "Use the browser_task tool with the goal: open https://example.com and read the main " +
      "heading. Then reply with that heading.",
  );
  // browser_task asks by default (architecture Section 10.2).
  const approved = await waitForReplyApproving(page, /Example Domain/i);
  expect(approved).toContain("browser_task");
});
