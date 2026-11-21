import { describe, expect, it } from "vitest";
import type { Org } from "./api";
import {
  AGENT_TEMPLATES,
  currentStep,
  hasActiveChatApp,
  loadProgress,
  onboardingSteps,
  saveProgress,
  showSetupCard,
  usableAgents,
  usableChoice,
} from "./onboarding";

function memory() {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
  };
}

const org = (over: Partial<Org> = {}) =>
  ({
    admin: true,
    plan: "free",
    integrations: [],
    agent_profiles: [{ id: "p1", name: "sama", workspace_id: null, runtime: "claude_agent_sdk", model: "m" }],
    ...over,
  }) as unknown as Org;

describe("progress", () => {
  it("keeps the progress of each org", () => {
    const store = memory();
    saveProgress(store, "o1", { channel: "skipped", agentId: "p1" });
    expect(loadProgress(store, "o1")).toEqual({ channel: "skipped", agentId: "p1" });
    expect(loadProgress(store, "o2")).toEqual({});
  });

  it("drops a damaged or unknown value", () => {
    const store = memory();
    store.setItem("sama.onboarding.o1", "{bad");
    expect(loadProgress(store, "o1")).toEqual({});
    store.setItem("sama.onboarding.o1", JSON.stringify({ channel: "x", agentId: 3, done: "yes" }));
    expect(loadProgress(store, "o1")).toEqual({});
  });
});

describe("steps", () => {
  it("starts at the plan step", () => {
    const steps = onboardingSteps(org(), {});
    expect(steps.map((s) => s.id)).toEqual(["org", "plan", "channel", "agent", "message"]);
    expect(steps.map((s) => s.done)).toEqual([true, false, false, false, false]);
    expect(currentStep(steps)).toBe("plan");
  });

  it("counts a kept plan or a paid plan as done", () => {
    expect(currentStep(onboardingSteps(org(), { plan: "kept" }))).toBe("channel");
    expect(currentStep(onboardingSteps(org({ plan: "pro" }), {}))).toBe("channel");
    const store = memory();
    saveProgress(store, "o1", { plan: "kept" });
    expect(loadProgress(store, "o1")).toEqual({ plan: "kept" });
  });

  it("counts an active chat app as done", () => {
    const active = org({ integrations: [{ kind: "slack", status: "active", name: "x" }] });
    expect(hasActiveChatApp(active)).toBe(true);
    expect(hasActiveChatApp(org({ integrations: [{ kind: "telegram", status: "pending", name: null }] }))).toBe(false);
    expect(currentStep(onboardingSteps(active, { plan: "kept" }))).toBe("agent");
  });

  it("needs an agent that still exists", () => {
    const base = { plan: "kept", channel: "skipped" } as const;
    expect(currentStep(onboardingSteps(org(), { ...base, agentId: "gone" }))).toBe("agent");
    expect(currentStep(onboardingSteps(org(), { ...base, agentId: "p1" }))).toBe("message");
    expect(currentStep(onboardingSteps(org(), { ...base, agentId: "p1", done: true }))).toBeNull();
  });

  it("shows the setup card only to an admin with open steps", () => {
    expect(showSetupCard(org(), {})).toBe(true);
    expect(showSetupCard(org({ admin: false }), {})).toBe(false);
    expect(showSetupCard(org(), { done: true })).toBe(false);
  });

  it("has templates with a short soul", () => {
    for (const t of AGENT_TEMPLATES) {
      expect(t.name.length).toBeLessThanOrEqual(80);
      expect(t.soul.length).toBeGreaterThan(20);
    }
  });
});

describe("agent choice", () => {
  const agents = [
    { id: "all", workspace_id: null },
    { id: "a-only", workspace_id: "A" },
    { id: "b-only", workspace_id: "B" },
  ];

  it("lists the agents that a workspace can use", () => {
    expect(usableAgents(agents, "A").map((a) => a.id)).toEqual(["all", "a-only"]);
    expect(usableAgents(agents, null).map((a) => a.id)).toEqual(["all"]);
  });

  it("drops a choice that the new workspace cannot use, and never swaps in another agent", () => {
    expect(usableChoice(agents, "A", "a-only")).toBe("a-only");
    expect(usableChoice(agents, "B", "a-only")).toBeNull();
    expect(usableChoice(agents, "B", "all")).toBe("all");
    expect(usableChoice(agents, "A", undefined)).toBeNull();
    expect(usableChoice([], "A", "all")).toBeNull();
  });
});
