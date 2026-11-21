import { describe, expect, it } from "vitest";
import type { Org } from "./api";
import { NEXT_STEPS, setupChecklist, setupProgressText } from "./setup";

type OrgPart = Pick<Org, "integrations" | "agent_profiles" | "plan">;

function org(overrides: Partial<OrgPart> = {}): OrgPart {
  return { integrations: [], agent_profiles: [], plan: "free", ...overrides };
}

const AGENT = {
  id: "a1",
  name: "Assistant",
  workspace_id: null,
  runtime: "claude_agent_sdk",
  model: "m",
} as unknown as Org["agent_profiles"][number];

describe("setupChecklist", () => {
  it("lists the five steps in order with a link for each step except the first", () => {
    const items = setupChecklist(org(), {});
    expect(items.map((i) => i.id)).toEqual(["org", "plan", "channel", "agent", "message"]);
    expect(items.map((i) => i.href)).toEqual([null, "/billing", "/console/slack", "/console/harness", "/"]);
  });

  it("marks the first open step as next and the later steps as to do", () => {
    const items = setupChecklist(org(), {});
    expect(items.map((i) => i.status)).toEqual(["done", "next", "todo", "todo", "todo"]);
  });

  it("marks a done step done", () => {
    const items = setupChecklist(
      org({
        plan: "team",
        integrations: [{ kind: "slack", status: "active", name: "Acme" }],
        agent_profiles: [AGENT],
      }),
      { agentId: "a1" },
    );
    expect(items.map((i) => i.status)).toEqual(["done", "done", "done", "done", "next"]);
  });

  it("marks a skipped chat app step as skipped", () => {
    const items = setupChecklist(org(), { plan: "kept", channel: "skipped" });
    expect(items.find((i) => i.id === "channel")?.status).toBe("skipped");
    expect(items.find((i) => i.id === "agent")?.status).toBe("next");
  });

  it("does not call a step skipped when a chat app is active", () => {
    const items = setupChecklist(
      org({ integrations: [{ kind: "telegram", status: "active", name: null }] }),
      { channel: "skipped" },
    );
    expect(items.find((i) => i.id === "channel")?.status).toBe("done");
  });

  it("has no next step when all steps are done", () => {
    const items = setupChecklist(org({ plan: "team", agent_profiles: [AGENT] }), {
      agentId: "a1",
      channel: "skipped",
      done: true,
    });
    expect(items.every((i) => i.done)).toBe(true);
    expect(items.some((i) => i.status === "next")).toBe(false);
  });
});

describe("setupProgressText", () => {
  it("counts the done steps", () => {
    expect(setupProgressText(setupChecklist(org(), {}))).toBe("1 of 5 steps done");
  });
});

describe("NEXT_STEPS", () => {
  it("links to the console sections that the guide names", () => {
    expect(NEXT_STEPS.map((s) => s.href)).toEqual([
      "/console/roles",
      "/console/tools",
      "/console/mcp",
      "/console/schedules",
      "/console/webhooks",
    ]);
  });
});
