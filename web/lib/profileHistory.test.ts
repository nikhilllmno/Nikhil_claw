import { describe, expect, it } from "vitest";
import type { ProfileChange } from "./api";
import { canRevert, changeTitle, kindLabel, sourceLabel } from "./profileHistory";

function change(overrides: Partial<ProfileChange> = {}): ProfileChange {
  return {
    id: "c1",
    kind: "toolsets",
    source: "proposal",
    summary: "add web",
    before: { toolsets: ["files"] },
    after: { toolsets: ["files", "web"] },
    diff: "toolsets:\n+ web",
    actor_user_id: "u1",
    actor_name: "Alice",
    proposal_id: "p1",
    reverts_change_id: null,
    reverted_by_change_id: null,
    created_at: "2026-11-21T10:00:00Z",
    ...overrides,
  };
}

describe("profile history labels", () => {
  it("names each kind and source", () => {
    expect(kindLabel("soul")).toBe("Soul");
    expect(kindLabel("policy")).toBe("Policy rules");
    expect(sourceLabel("edit")).toBe("Edit");
    expect(sourceLabel("revert")).toBe("Revert");
  });

  it("keeps an unknown value as it is", () => {
    expect(kindLabel("other")).toBe("other");
    expect(sourceLabel("other")).toBe("other");
  });

  it("puts the kind, the source, and the person in the title", () => {
    expect(changeTitle(change())).toBe("Tools · Approved proposal by Alice");
    expect(changeTitle(change({ actor_name: null, source: "edit" }))).toBe("Tools · Edit");
  });
});

describe("canRevert", () => {
  it("allows an editor to revert a change that is not reverted", () => {
    expect(canRevert(change(), true)).toBe(true);
  });

  it("refuses a reader", () => {
    expect(canRevert(change(), false)).toBe(false);
  });

  it("refuses a change that is already reverted", () => {
    expect(canRevert(change({ reverted_by_change_id: "c2" }), true)).toBe(false);
  });
});
