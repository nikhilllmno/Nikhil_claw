// M4.1 to M4.3: a cron job from the Jobs page: the schedule preview, a new job, and Run now.

import { poll } from "../lib/api.ts";
import { marker } from "../lib/names.ts";
import { expect, test } from "../fixtures.ts";

interface Job {
  id: string;
  name: string;
  status: string;
  fires: { status: string; manual: boolean; output: string | null; session_id?: string | null }[];
}

test("preview a schedule, make a job, and run it now", async ({ ownerPage: page, ownerApi, world }) => {
  const word = marker("job");
  const name = `e2e job ${word}`;
  await page.goto("/jobs");
  await page.getByRole("button", { name: "New job" }).first().click();
  await page.getByRole("textbox", { name: /^Name/ }).fill(name);
  await page.getByRole("textbox", { name: /^Schedule/ }).fill("every day at 9am");
  await page.getByRole("combobox", { name: /^Time zone/ }).fill("Asia/Kolkata");
  // The preview shows the next fire times.
  await expect(page.locator("#schedule-preview")).toContainText(/\d/);
  await expect(page.locator("#schedule-preview").getByRole("alert")).toHaveCount(0);
  await page.getByRole("textbox", { name: /^Prompt/ }).fill(`Reply with the word ${word} and nothing else.`);
  await page.getByRole("textbox", { name: /^Send the result to/ }).fill("none");
  await page.getByRole("button", { name: "Make the job" }).click();

  await expect(page.getByRole("heading", { name })).toBeVisible();
  await page.getByRole("button", { name: "Run now" }).click();
  await expect(page.getByText("The job started.", { exact: false })).toBeVisible();

  const jobs = await ownerApi.get<Job[] | { jobs: Job[] }>(`/orgs/${world.org.id}/jobs`);
  const job = (Array.isArray(jobs) ? jobs : jobs.jobs).find((j) => j.name === name);
  expect(job?.status).toBe("active");
  try {
    const fire = await poll(
      "the manual fire of the job to end",
      async () => {
        const view = await ownerApi.get<Job>(`/orgs/${world.org.id}/jobs/${job!.id}`);
        const done = view.fires.find((f) => f.manual && f.status !== "running");
        return done;
      },
      { timeoutMs: 4 * 60_000, intervalMs: 5_000 },
    );
    expect(fire.status).toBe("completed");
    expect(fire.output ?? "").toContain(word);
    await page.getByRole("button", { name: "Refresh" }).click();
    await expect(page.getByText(word).first()).toBeVisible();
  } finally {
    await ownerApi.delete(`/orgs/${world.org.id}/jobs/${job!.id}`);
  }
});
