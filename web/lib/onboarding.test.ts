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
  it("starts at the chat app step", () => {
    const steps = onboardingSteps(org(), {});
    expect(steps.map((s) => s.done)).toEqual([true, false, false, false]);
    expect(currentStep(steps)).toBe("channel");
  });

  it("counts an active chat app as done", () => {
    const active = org({ integrations: [{ kind: "slack", status: "active", name: "x" }] });
    expect(hasActiveChatApp(active)).toBe(true);
    expect(hasActiveChatApp(org({ integrations: [{ kind: "telegram", status: "pending", name: null }] }))).toBe(false);
    expect(currentStep(onboardingSteps(active, {}))).toBe("agent");
  });

  it("needs an agent that still exists", () => {
    expect(currentStep(onboardingSteps(org(), { channel: "skipped", agentId: "gone" }))).toBe("agent");
    expect(currentStep(onboardingSteps(org(), { channel: "skipped", agentId: "p1" }))).toBe("message");
    expect(currentStep(onboardingSteps(org(), { channel: "skipped", agentId: "p1", done: true }))).toBeNull();
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
