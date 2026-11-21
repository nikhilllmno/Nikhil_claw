import { describe, expect, it } from "vitest";
import type { Trigger, Workflow } from "./api";
import {
  buildNewTrigger,
  buildTriggerEdit,
  canManageTrigger,
  deliverySummary,
  isGoodOutcome,
  kindNoun,
  newTriggerDraft,
  outcomeLabel,
  outcomeTone,
  parseConfig,
  parsePayload,
  targetText,
  toTriggerDraft,
  triggerStatusLabel,
  triggerStatusTone,
  validateNewTrigger,
  validateTriggerEdit,
  workflowChoices,
} from "./triggers";

function trigger(overrides: Partial<Trigger> = {}): Trigger {
  return {
    id: "t1",
    workspace_id: "w1",
    owner_user_id: "u1",
    agent_profile_id: "p1",
    kind: "webhook",
    name: "Deploys",
    prompt: "Summarise the event.",
    toolsets: ["web"],
    target: "run",
    workflow_id: null,
    status: "active",
    last_error: null,
    created_at: "2026-11-20T00:00:00+00:00",
    updated_at: "2026-11-20T00:00:00+00:00",
    url: "https://sama.example/triggers/t1/webhook",
    ...overrides,
  };
}

describe("labels", () => {
  it("names the kinds and the statuses", () => {
    expect(kindNoun("webhook")).toBe("webhook");
    expect(kindNoun("composio")).toBe("trigger");
    expect(triggerStatusLabel("active")).toBe("Active");
    expect(triggerStatusLabel("disabled")).toBe("Disabled");
    expect(triggerStatusTone("active")).toBe("accent");
    expect(triggerStatusTone("disabled")).toBe("neutral");
  });

  it("names each outcome of a delivery", () => {
    expect(outcomeLabel("accepted")).toBe("Accepted");
    expect(outcomeLabel("bad_signature")).toBe("Bad signature");
    expect(outcomeLabel("test_ok")).toBe("Test passed");
    expect(outcomeLabel("rate_limited")).toBe("Too many requests");
    expect(outcomeTone("accepted")).toBe("accent");
    expect(outcomeTone("test_ok")).toBe("accent");
    expect(outcomeTone("duplicate")).toBe("warning");
    expect(outcomeTone("disabled")).toBe("warning");
    expect(outcomeTone("bad_signature")).toBe("danger");
    expect(outcomeTone("rejected")).toBe("danger");
    expect(isGoodOutcome("accepted")).toBe(true);
    expect(isGoodOutcome("bad_body")).toBe(false);
  });

  it("writes a sentence about a delivery", () => {
    expect(deliverySummary({ outcome: "test_ok", http_status: 200, error: null })).toBe("Test passed (HTTP 200).");
    expect(deliverySummary({ outcome: "bad_body", http_status: 400, error: "The body is not JSON." })).toBe(
      "Bad body (HTTP 400). The body is not JSON.",
    );
  });

  it("says what a trigger starts", () => {
    expect(targetText({ target: "run" }, "Assistant", null)).toBe("Starts a run of Assistant");
    expect(targetText({ target: "run" }, null, null)).toBe("Starts a run");
    expect(targetText({ target: "workflow" }, null, "digest")).toBe("Starts the workflow digest");
    expect(targetText({ target: "workflow" }, null, null)).toBe("Starts a workflow");
  });
});

describe("permissions and choices", () => {
  it("lets an admin and the owner change a trigger", () => {
    expect(canManageTrigger({ admin: true, user_id: "u9" }, { owner_user_id: "u1" })).toBe(true);
    expect(canManageTrigger({ admin: false, user_id: "u1" }, { owner_user_id: "u1" })).toBe(true);
    expect(canManageTrigger({ admin: false, user_id: "u2" }, { owner_user_id: "u1" })).toBe(false);
  });

  it("offers the workflows of the workspace that are not removed", () => {
    const flow = (id: string, workspace_id: string, status: Workflow["status"]) =>
      ({ id, workspace_id, status }) as Workflow;
    const list = [flow("a", "w1", "active"), flow("b", "w2", "active"), flow("c", "w1", "removed"), flow("d", "w1", "paused")];
    expect(workflowChoices(list, "w1").map((w) => w.id)).toEqual(["a", "d"]);
  });
});

describe("payload and config", () => {
  it("parses a payload", () => {
    expect(parsePayload("")).toEqual({ ok: true, payload: null });
    expect(parsePayload('{"a":1}')).toEqual({ ok: true, payload: { a: 1 } });
    expect(parsePayload("[1]").ok).toBe(false);
    expect(parsePayload("{").ok).toBe(false);
    const big = JSON.stringify({ x: "a".repeat(20_001) });
    expect(parsePayload(big)).toEqual({ ok: false, error: "The payload has more than 20,000 bytes." });
  });

  it("parses a config", () => {
    expect(parseConfig("")).toEqual({ ok: true, config: null });
    expect(parseConfig('{"repo":"x"}')).toEqual({ ok: true, config: { repo: "x" } });
    expect(parseConfig("nope")).toEqual({ ok: false, error: "The config is not valid JSON." });
  });
});

describe("new trigger", () => {
  it("needs a name, an agent, and a prompt for a run", () => {
    const draft = newTriggerDraft("p1");
    expect(validateNewTrigger(draft, "webhook")).toBe("The webhook needs a name.");
    draft.name = "Deploys";
    expect(validateNewTrigger(draft, "webhook")).toContain("prompt");
    draft.prompt = "Do it.";
    expect(validateNewTrigger(draft, "webhook")).toBeNull();
    expect(validateNewTrigger({ ...draft, agentId: "" }, "webhook")).toBe("Choose an agent.");
    expect(validateNewTrigger({ ...draft, name: "x".repeat(81) }, "webhook")).toContain("at most 80");
  });

  it("needs a workflow for a workflow target", () => {
    const draft = { ...newTriggerDraft("p1"), name: "n", target: "workflow" as const };
    expect(validateNewTrigger(draft, "webhook")).toBe("Choose a workflow.");
    expect(validateNewTrigger({ ...draft, workflowId: "f1" }, "webhook")).toBeNull();
  });

  it("needs a connection, a type, and a valid config for a Composio trigger", () => {
    const draft = { ...newTriggerDraft("p1"), name: "n", prompt: "p" };
    expect(validateNewTrigger(draft, "composio")).toBe("Choose a connected app.");
    const withConnection = { ...draft, connectionId: "c1" };
    expect(validateNewTrigger(withConnection, "composio")).toContain("trigger type");
    const withSlug = { ...withConnection, slug: "GITHUB_COMMIT_EVENT" };
    expect(validateNewTrigger(withSlug, "composio")).toBeNull();
    expect(validateNewTrigger({ ...withSlug, config: "{" }, "composio")).toBe("The config is not valid JSON.");
  });

  it("builds the request of a run", () => {
    const draft = { ...newTriggerDraft("p1"), name: " Deploys ", prompt: " Do it. " };
    expect(buildNewTrigger(draft, "webhook")).toEqual({
      kind: "webhook",
      name: "Deploys",
      agent_profile_id: "p1",
      prompt: "Do it.",
      toolsets: ["web", "media", "clarify"],
    });
  });

  it("builds the request of a workflow with no agent and no prompt", () => {
    const draft = { ...newTriggerDraft("p1"), name: "n", target: "workflow" as const, workflowId: "f1" };
    expect(buildNewTrigger(draft, "webhook")).toEqual({ kind: "webhook", name: "n", workflow_id: "f1" });
  });

  it("builds the request of a Composio trigger", () => {
    const draft = {
      ...newTriggerDraft("p1"),
      name: "n",
      prompt: "p",
      connectionId: "c1",
      slug: " GITHUB_COMMIT_EVENT ",
      config: '{"repo":"x"}',
    };
    expect(buildNewTrigger(draft, "composio")).toMatchObject({
      kind: "composio",
      connection_id: "c1",
      composio_slug: "GITHUB_COMMIT_EVENT",
      composio_config: { repo: "x" },
    });
    expect(buildNewTrigger({ ...draft, config: "" }, "composio")).not.toHaveProperty("composio_config");
  });
});

describe("edit of a trigger", () => {
  it("sends only the changed fields", () => {
    const t = trigger();
    expect(buildTriggerEdit(t, toTriggerDraft(t))).toEqual({});
    expect(buildTriggerEdit(t, { ...toTriggerDraft(t), name: "New" })).toEqual({ name: "New" });
    expect(buildTriggerEdit(t, { ...toTriggerDraft(t), prompt: "Other." })).toEqual({ prompt: "Other." });
    expect(buildTriggerEdit(t, { ...toTriggerDraft(t), toolsets: ["web", "core"] })).toEqual({
      toolsets: ["web", "core"],
    });
  });

  it("does not send a prompt or tool groups for a workflow trigger", () => {
    const t = trigger({ target: "workflow", workflow_id: "f1", prompt: "Start the workflow x." });
    const draft = { ...toTriggerDraft(t), prompt: "Changed", toolsets: ["core"] };
    expect(buildTriggerEdit(t, draft)).toEqual({});
  });

  it("checks the edit", () => {
    const t = trigger();
    expect(validateTriggerEdit(toTriggerDraft(t), t)).toBeNull();
    expect(validateTriggerEdit({ ...toTriggerDraft(t), name: " " }, t)).toBe("The webhook needs a name.");
    expect(validateTriggerEdit({ ...toTriggerDraft(t), prompt: "" }, t)).toContain("prompt");
    const flow = trigger({ target: "workflow", prompt: null });
    expect(validateTriggerEdit({ ...toTriggerDraft(flow), prompt: "" }, flow)).toBeNull();
  });

  it("reads a Composio trigger into a draft", () => {
    const t = trigger({
      kind: "composio",
      connection_id: "c1",
      composio_slug: "SLUG",
      composio_config: { a: 1 },
      url: undefined,
    });
    expect(toTriggerDraft(t)).toMatchObject({ connectionId: "c1", slug: "SLUG", config: '{"a":1}' });
  });
});
