// M7.1: busy modes. The default `steer` lets a plain message join the running run. `/bg TASK`
// starts a parallel run in the same session, and `/stop NUMBER` stops one of the open runs.

import { poll, type RunOut } from "../lib/api.ts";
import { marker } from "../lib/names.ts";
import { composer, send, startSession, waitForReply, waitIdle } from "../lib/ui.ts";
import { expect, test } from "../fixtures.ts";

/** A prompt that keeps a run busy for some tool steps, so a second message comes while it works. */
function busyTask(word: string): string {
  return (
    "Use the session_search tool four times, one call at a time, with the queries alpha, " +
    `beta, gamma, and delta. Then reply with the word ${word}.`
  );
}

interface RunTimes extends RunOut {
  started_at: string | null;
  finished_at: string | null;
}

test("a message during a run joins it (steer is the default)", async ({ ownerPage: page, ownerApi }) => {
  const first = marker("first");
  const join = marker("join");
  const sessionId = await startSession(page, `e2e join ${first}`);
  await send(page, busyTask(first));
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeVisible();
  await send(page, `Also put the word ${join} at the end of your reply.`);
  // The message shows as a note of the run, not as a new request.
  const note = page.getByRole("article", { name: "You, added to the current run" });
  await expect(note).toContainText(join);
  await expect(note).toContainText(/Queued into the current run|Delivered to the run/);
  await waitForReply(page, first);
  await waitForReply(page, join);
  await waitIdle(page);
  // The note ends as delivered, or as the next message when the run ended before its next step.
  const steers = await ownerApi.get<{ text: string; status: string }[]>(`/sessions/${sessionId}/steers`);
  const status = steers.find((s) => s.text.includes(join))?.status;
  expect(status).toMatch(/^(delivered|requeued)$/);
  if (status === "delivered") {
    await expect(note).toContainText("Delivered to the run");
  } else {
    await expect(page.getByRole("article", { name: "You", exact: true }).filter({ hasText: join })).toBeVisible();
  }
});

test("/bg starts a second run at the same time", async ({ ownerPage: page, ownerApi }) => {
  const first = marker("main");
  const side = marker("side");
  const sessionId = await startSession(page, `e2e parallel ${first}`);
  await send(page, busyTask(first));
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeVisible();
  await send(page, `/bg Reply with the word ${side} and nothing else.`);
  await expect(page.getByRole("region", { name: "Runs that work at the same time" })).toBeVisible();
  await waitForReply(page, side);
  await waitForReply(page, first);
  await waitIdle(page);

  // The two runs were open at the same time.
  const runs = (await ownerApi.runs(sessionId)) as RunTimes[];
  const chat = runs.filter((r) => r.kind === "chat" && r.started_at && r.finished_at);
  expect(chat.length).toBeGreaterThanOrEqual(2);
  const [newer, older] = [chat[0]!, chat[1]!];
  expect(Date.parse(newer.started_at!)).toBeLessThan(Date.parse(older.finished_at!));
});

test("the Run in parallel button starts a second run", async ({ ownerPage: page }) => {
  const first = marker("slow");
  const quick = marker("quick");
  await startSession(page, `e2e parallel button ${first}`);
  await send(page, busyTask(first));
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeVisible();
  await composer(page).fill(`Reply with the word ${quick} and nothing else.`);
  await page.getByRole("button", { name: "Run in parallel" }).click();
  const runs = page.getByRole("region", { name: "Runs that work at the same time" });
  await expect(runs).toBeVisible();
  await expect(runs.getByRole("button", { name: /^Stop run \d+$/ })).toHaveCount(2);
  await waitForReply(page, quick);
  await waitForReply(page, first);
  await waitIdle(page);
});

test("/stop NUMBER stops one of two open runs", async ({ ownerPage: page, ownerApi }) => {
  const sessionId = await startSession(page, `e2e stop one ${marker("s")}`);
  await send(page, busyTask(marker("keep")));
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeVisible();
  await send(page, "/bg Use the session_search tool four times, one call at a time, then reply DONE.");
  const runs = page.getByRole("region", { name: "Runs that work at the same time" });
  await expect(runs).toBeVisible();
  await expect(runs.getByRole("button", { name: "Stop run 2" })).toBeVisible();
  await send(page, "/stop 2");
  const ended = await poll(
    "one cancelled run and one other run",
    async () => {
      const all = await ownerApi.runs(sessionId);
      const cancelled = all.filter((r) => r.status === "cancelled");
      return cancelled.length === 1 && all.some((r) => r.status !== "cancelled") ? all : undefined;
    },
    { timeoutMs: 60_000 },
  );
  expect(ended.filter((r) => r.status === "cancelled")).toHaveLength(1);
  await waitIdle(page);
  const final = await ownerApi.runs(sessionId);
  expect(final.filter((r) => r.status === "completed")).toHaveLength(1);
});
