import { describe, expect, it } from "vitest";
import type { RolePermissions } from "./api";
import {
  appliesTo,
  cellLabel,
  cellValue,
  changedCount,
  changedPermissions,
  permissionNote,
  revokedLabels,
  savedValue,
  toggleDraft,
} from "./rolePermissions";

function data(overrides: Partial<RolePermissions> = {}): RolePermissions {
  return {
    permissions: [
      { key: "jobs.create", label: "Create scheduled jobs", description: "", roles: ["admin", "member"] },
      { key: "skills.write", label: "Create and change skills", description: "", roles: ["admin", "member"] },
      { key: "members.invite", label: "Invite people", description: "", roles: ["admin"] },
    ],
    roles: {
      admin: { "jobs.create": true, "skills.write": true, "members.invite": true },
      member: { "jobs.create": true, "skills.write": false },
    },
    editable: { admin: false, member: true },
    ...overrides,
  };
}

describe("savedValue and cellValue", () => {
  it("reads the saved value", () => {
    expect(savedValue(data(), "member", "skills.write")).toBe(false);
    expect(savedValue(data(), "member", "jobs.create")).toBe(true);
  });

  it("treats a missing key as allowed", () => {
    expect(savedValue(data({ roles: { admin: {}, member: {} } }), "member", "jobs.create")).toBe(true);
  });

  it("prefers the draft", () => {
    expect(cellValue(data(), "member", "skills.write", { "skills.write": true })).toBe(true);
    expect(cellValue(data(), "member", "jobs.create", {})).toBe(true);
  });
});

describe("appliesTo", () => {
  it("is false for a key that is not for the role", () => {
    const info = data().permissions[2]!;
    expect(appliesTo(info, "admin")).toBe(true);
    expect(appliesTo(info, "member")).toBe(false);
  });
});

describe("toggleDraft", () => {
  it("adds a changed value to the draft", () => {
    expect(toggleDraft(data(), "member", {}, "jobs.create", false)).toEqual({ "jobs.create": false });
  });

  it("removes the key when the value goes back to the saved value", () => {
    expect(toggleDraft(data(), "member", { "jobs.create": false }, "jobs.create", true)).toEqual({});
  });

  it("does not change the draft it gets", () => {
    const draft = {};
    toggleDraft(data(), "member", draft, "jobs.create", false);
    expect(draft).toEqual({});
  });
});

describe("changedPermissions", () => {
  it("sends only the keys that changed", () => {
    expect(
      changedPermissions(data(), "member", { "jobs.create": false, "skills.write": false }),
    ).toEqual({ "jobs.create": false });
  });

  it("leaves out a key that is not for the role", () => {
    expect(changedPermissions(data(), "member", { "members.invite": false })).toEqual({});
  });

  it("is empty for an empty draft", () => {
    expect(changedPermissions(data(), "admin", {})).toEqual({});
    expect(changedCount(data(), "admin", {})).toBe(0);
  });

  it("counts the changes", () => {
    expect(changedCount(data(), "admin", { "jobs.create": false, "members.invite": false })).toBe(2);
  });
});

describe("revokedLabels", () => {
  it("names only the permissions that the role loses", () => {
    expect(
      revokedLabels(data(), "member", { "jobs.create": false, "skills.write": true }),
    ).toEqual(["Create scheduled jobs"]);
  });
});

describe("permissionNote", () => {
  it("says who can change the table", () => {
    expect(permissionNote({ admin: true, member: true })).toContain("both columns");
    expect(permissionNote({ admin: false, member: true })).toContain("Member column");
    expect(permissionNote({ admin: false, member: false })).toContain("Only an org owner");
  });
});

describe("cellLabel", () => {
  it("names the permission and the role", () => {
    expect(cellLabel({ label: "Invite people" }, "admin")).toBe("Invite people: Admin");
  });
});
