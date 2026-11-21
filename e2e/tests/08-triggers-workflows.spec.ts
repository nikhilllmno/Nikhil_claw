// M4.4: a signed webhook starts a run. M4.5: a workflow runs to the end. These flows have no
// page in the web app yet, so the test uses the API and checks the result in the web app.

import { randomUUID } from "node:crypto";
import { poll, type SessionOut, waitForRuns } from "../lib/api.ts";
import { marker } from "../lib/names.ts";
import { waitForReply } from "../lib/ui.ts";
import { webhookHeaders } from "../lib/webhook.ts";
import { expect, test } from "../fixtures.ts";

test("a signed webhook starts a run, and a bad signature does not", async ({
  ownerPage: page,
  ownerApi,
  world,
  request,
}) => {
  const word = marker("hook");
  const name = `e2e-hook-${word.toLowerCase()}`;
  const trigger = await ownerApi.post<{ id: string; secret: string }>(
    `/workspaces/${world.org.workspaceId}/triggers`,
    {
      kind: "webhook",
      name,
      prompt: `Reply with the word ${word} and nothing else.`,
      agent_profile_id: world.org.profileId,
    },
  );
  try {
    expect(trigger.secret).toMatch(/^whsec_/);
    const url = `${world.apiUrl}/triggers/${trigger.id}/webhook`;
    const body = JSON.stringify({ event: "e2e", word });

    const bad = await request.post(url, {
      data: body,
      headers: webhookHeaders(`whsec_${Buffer.from("wrong key").toString("base64")}`, randomUUID(), body),
    });
    expect(bad.status()).toBe(401);

    const id = randomUUID();
    const good = await request.post(url, { data: body, headers: webhookHeaders(trigger.secret, id, body) });
    expect(good.status()).toBe(202);
    // The same delivery again is a duplicate and starts nothing.
    const again = await request.post(url, { data: body, headers: webhookHeaders(trigger.secret, id, body) });
    expect(again.status()).toBe(200);
    expect(await again.json()).toMatchObject({ duplicate: true });

    const session = await poll<SessionOut>("the session of the trigger", async () =>
      (await ownerApi.sessions()).find((s) => s.title === `Trigger: ${name}`),
    );
    const runs = await waitForRuns(ownerApi, session.id);
    expect(runs.map((r) => r.status)).toEqual(["completed"]);
    await page.goto(`/s/${session.id}`);
    await waitForReply(page, word, 30_000);
  } finally {
    await ownerApi.delete(`/workspaces/${world.org.workspaceId}/triggers/${trigger.id}`);
  }
});

test("a workflow with a wait step runs to the end", async ({ ownerApi, world }) => {
  const name = `e2e-flow-${marker("w").toLowerCase()}`;
  const yaml = `name: ${name}\nsteps:\n  - id: pause\n    wait:\n      seconds: 2\n`;
  const checked = await ownerApi.call("/workflows/validate", { method: "POST", body: { yaml } });
  expect(checked.status).toBe(200);
  const workflow = await ownerApi.post<{ id: string; version: number; needs_approval: boolean }>(
    `/orgs/${world.org.id}/workflows`,
    { yaml, workspace_id: world.org.workspaceId, agent_profile_id: world.org.profileId },
  );
  try {
    expect(workflow.needs_approval).toBe(false);
    const instance = await ownerApi.post<{ id: string }>(
      `/orgs/${world.org.id}/workflows/${workflow.id}/start`,
      { inputs: {} },
    );
    const done = await poll(
      "the workflow instance to end",
      async () => {
        const view = await ownerApi.get<{ status: string }>(
          `/orgs/${world.org.id}/workflow-instances/${instance.id}`,
        );
        return ["completed", "failed", "cancelled"].includes(view.status) ? view : undefined;
      },
      { timeoutMs: 3 * 60_000, intervalMs: 3_000 },
    );
    expect(done.status).toBe("completed");
  } finally {
    await ownerApi.delete(`/orgs/${world.org.id}/workflows/${workflow.id}`);
  }
});
