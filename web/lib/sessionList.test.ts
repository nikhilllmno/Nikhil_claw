import { describe, expect, it } from "vitest";
import type { SessionOut } from "./api";
import {
  archiveConfirmText,
  canManageSession,
  checkTitle,
  emptyText,
  maybeTruncated,
  replaceSession,
  sessionsOfWorkspace,
  titleChanged,
  withoutSession,
} from "./sessionList";

function session(overrides: Partial<SessionOut> = {}): SessionOut {
  return {
    id: "s1",
    org_id: "o1",
    workspace_id: "w1",
    agent_profile_id: "a1",
    audience: "private",
    title: "Plan",
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
    lineage: null,
    parent_session_id: null,
    ...overrides,
  };
}

describe("sessionsOfWorkspace", () => {
  it("keeps the sessions of the workspace", () => {
    const list = [session({ id: "a" }), session({ id: "b", workspace_id: "w2" })];
    expect(sessionsOfWorkspace(list, "w1").map((s) => s.id)).toEqual(["a"]);
  });

  it("leaves out the archive of a compaction", () => {
    const list = [session({ id: "a" }), session({ id: "b", lineage: "compaction" })];
    expect(sessionsOfWorkspace(list, "w1").map((s) => s.id)).toEqual(["a"]);
  });

  it("is empty without a workspace", () => {
    expect(sessionsOfWorkspace([session()], null)).toEqual([]);
  });
});

describe("checkTitle", () => {
  it("trims the title", () => {
    expect(checkTitle("  New name ")).toEqual({ title: "New name" });
  });

  it("refuses an empty title", () => {
    expect(checkTitle("   ")).toEqual({ error: "Give the session a title." });
  });

  it("refuses a long title", () => {
    expect(checkTitle("a".repeat(201))).toHaveProperty("error");
    expect(checkTitle("a".repeat(200))).toHaveProperty("title");
  });
});

describe("titleChanged", () => {
  it("is true for a new non-empty title", () => {
    expect(titleChanged("Plan", "Plan 2")).toBe(true);
    expect(titleChanged(null, "Plan")).toBe(true);
  });

  it("is false for the same or an empty title", () => {
    expect(titleChanged("Plan", " Plan ")).toBe(false);
    expect(titleChanged("Plan", "  ")).toBe(false);
  });
});

describe("list changes", () => {
  it("removes a session", () => {
    expect(withoutSession([session({ id: "a" }), session({ id: "b" })], "a").map((s) => s.id)).toEqual(["b"]);
  });

  it("replaces a session", () => {
    const list = replaceSession([session({ id: "a" }), session({ id: "b" })], session({ id: "b", title: "New" }));
    expect(list.map((s) => s.title)).toEqual(["Plan", "New"]);
  });
});

describe("text", () => {
  it("names the session in the confirm text", () => {
    expect(archiveConfirmText("Plan")).toContain("Archive Plan?");
  });

  it("has a line for each empty list", () => {
    expect(emptyText("active", "Main")).toBe("Main has no active sessions.");
    expect(emptyText("archived", "Main")).toBe("Main has no archived sessions.");
  });

  it("knows when the list is full", () => {
    expect(maybeTruncated(100)).toBe(true);
    expect(maybeTruncated(99)).toBe(false);
  });
});

describe("canManageSession", () => {
  it("follows can_manage", () => {
    expect(canManageSession({ can_manage: true })).toBe(true);
    expect(canManageSession({ can_manage: false })).toBe(false);
  });

  it("treats a missing can_manage as true", () => {
    expect(canManageSession({})).toBe(true);
  });
});
