import { describe, expect, it } from "vitest";
import type { ProfileChange } from "./api";
import { changeTitle } from "./profileHistory";

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
    redacted: false,
    created_at: "2026-11-21T10:00:00Z",
    ...overrides,
  };
}

describe("profile history labels", () => {
  it("names each kind and source", () => {
    expect(changeTitle(change({ kind: "soul", source: "revert" }))).toBe("Soul · Revert by Alice");
    expect(changeTitle(change({ kind: "policy", source: "edit" }))).toBe(
      "Policy rules · Edit by Alice",
    );
    expect(changeTitle(change({ kind: "profile" }))).toBe("Settings · Approved proposal by Alice");
  });

  it("puts the kind, the source, and the person in the title", () => {
    expect(changeTitle(change())).toBe("Tools · Approved proposal by Alice");
    expect(changeTitle(change({ actor_name: null, source: "edit" }))).toBe("Tools · Edit");
  });
});
