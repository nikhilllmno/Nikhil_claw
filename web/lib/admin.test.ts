import { describe, expect, it } from "vitest";
import type { DataSettings, ProfileChange } from "./api";
import {
  auditActor,
  auditDetails,
  auditLabel,
  confirmsSlug,
  costShare,
  dayBars,
  daysText,
  mergeChanges,
  reportTitle,
  retentionChoice,
  retentionChoices,
  retentionSummary,
  shortDay,
} from "./admin";

const day = (d: string, cost: string) => ({ day: d, cost_usd: cost, input_tokens: 0, output_tokens: 0, model_calls: 0 });

describe("usage", () => {
  it("scales the bars to the most expensive day", () => {
    const bars = dayBars([day("2026-11-01", "0.5"), day("2026-11-02", "2.0"), day("2026-11-03", "0")]);
    expect(bars.map((b) => b.percent)).toEqual([25, 100, 0]);
    expect(bars[1]?.cost).toBe(2);
  });

  it("gives empty bars when nothing was spent", () => {
    expect(dayBars([day("2026-11-01", "0"), day("2026-11-02", "x")]).map((b) => b.percent)).toEqual([0, 0]);
  });

  it("gives the share of a row", () => {
    expect(costShare({ cost_usd: "1" }, "4")).toBe(25);
    expect(costShare({ cost_usd: "1" }, "0")).toBe(0);
    expect(costShare({ cost_usd: "9" }, "4")).toBe(100);
  });

  it("shows a UTC day as a short date", () => {
    expect(shortDay("2026-11-21")).toBe("21 Nov");
    expect(shortDay("bad")).toBe("bad");
  });
});

describe("audit", () => {
  it("labels known and unknown actions", () => {
    expect(auditLabel("member.role_changed")).toBe("Changed a role");
    expect(auditLabel("billing.plan_changed")).toBe("Billing plan changed");
  });

  it("names the actor", () => {
    expect(auditActor({ actor_kind: "user", actor_name: "Bob", actor_user_id: "u" })).toBe("Bob");
    expect(auditActor({ actor_kind: "system", actor_name: null, actor_user_id: null })).toBe("Sama");
    expect(auditActor({ actor_kind: "agent", actor_name: null, actor_user_id: null })).toBe("An agent");
    expect(auditActor({ actor_kind: "user", actor_name: null, actor_user_id: "u" })).toBe("A removed user");
  });

  it("keeps short details and leaves out nested values", () => {
    const parts = auditDetails({
      role: "admin",
      nested: { a: 1 },
      fields: ["soul", "name"],
      empty: [],
      long: "x".repeat(80),
      flag: true,
      to: null,
    });
    expect(parts).toEqual(["role: admin", "fields: soul, name", "empty: none", `long: ${"x".repeat(57)}…`]);
    expect(auditDetails({ a: 1, b: null }, 4)).toEqual(["a: 1", "b: none"]);
  });
});

describe("retention", () => {
  const settings: DataSettings = {
    plan: "free",
    retention_days: null,
    plan_default_days: null,
    plan_max_days: null,
    effective_days: null,
  };

  it("writes days", () => {
    expect(daysText(null)).toBe("Keep everything");
    expect(daysText(1)).toBe("1 day");
    expect(daysText(90)).toBe("90 days");
    expect(daysText(365)).toBe("1 year");
    expect(daysText(730)).toBe("2 years");
  });

  it("offers only the choices that the plan allows, and keeps the current value", () => {
    expect(retentionChoices(settings)).toEqual([null, 30, 90, 180, 365, 730]);
    expect(retentionChoices({ ...settings, plan_max_days: 90 })).toEqual([null, 30, 90]);
    expect(retentionChoices({ ...settings, retention_days: 45 })).toEqual([null, 30, 45, 90, 180, 365, 730]);
  });

  it("labels the plan default and summarizes the setting", () => {
    expect(retentionChoice(null, settings)).toBe("Plan default (keep everything)");
    expect(retentionChoice(null, { plan_default_days: 365 })).toBe("Plan default (1 year)");
    expect(retentionSummary(settings)).toBe("Sama keeps every session until someone deletes it.");
    expect(retentionSummary({ ...settings, effective_days: 30 })).toBe(
      "Sama deletes a session after 30 days without activity.",
    );
  });
});

describe("other helpers", () => {
  it("needs the exact slug to delete an org", () => {
    expect(confirmsSlug(" acme ", "acme")).toBe(true);
    expect(confirmsSlug("Acme", "acme")).toBe(false);
    expect(confirmsSlug("", "")).toBe(false);
  });

  it("merges the changes of the agents, newest first", () => {
    const change = (id: string, at: string) => ({ id, created_at: at }) as ProfileChange;
    const merged = mergeChanges(
      [
        { profileId: "a", profileName: "A", changes: [change("1", "2026-11-03T00:00:00Z"), change("2", "2026-11-01T00:00:00Z")] },
        { profileId: "b", profileName: "B", changes: [change("3", "2026-11-02T00:00:00Z")] },
      ],
      2,
    );
    expect(merged.map((c) => [c.id, c.profile_name])).toEqual([
      ["1", "A"],
      ["3", "B"],
    ]);
  });

  it("takes the first line of a report", () => {
    expect(reportTitle("\n# Curator report\n\nmore", "x")).toBe("Curator report");
    expect(reportTitle(null, "fallback")).toBe("fallback");
  });
});
