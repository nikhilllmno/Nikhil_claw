import { describe, expect, it } from "vitest";
import type { SessionOut } from "./api";
import {
  anyOpen,
  filterSessions,
  runStateLabel,
  directAgent,
  directBadge,
  directChoice,
  hasDirectMode,
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

describe("direct mode", () => {
  it("shows the control only when the API gives the mode and the session is active", () => {
    expect(hasDirectMode({ status: "active" })).toBe(false);
    expect(hasDirectMode({ status: "active", coding_agent: null })).toBe(true);
    expect(hasDirectMode({ status: "archived", coding_agent: "codex" })).toBe(false);
  });

  it("maps the choice to the body and back", () => {
    expect(directChoice({ coding_agent: null })).toBe("sama");
    expect(directChoice({ coding_agent: "claude_code" })).toBe("claude_code");
    expect(directAgent("sama")).toBeNull();
    expect(directAgent("codex")).toBe("codex");
  });

  it("gives a badge only in the direct mode", () => {
    expect(directBadge({ coding_agent: null })).toBeNull();
    expect(directBadge({})).toBeNull();
    expect(directBadge({ coding_agent: "claude_code" })).toBe("Direct: Claude Code");
    expect(directBadge({ coding_agent: "codex" })).toBe("Direct: Codex");
  });
});

describe("sidebar helpers (M8.2)", () => {
  it("labels the run state", () => {
    expect(runStateLabel("running")).toBe("Working");
    expect(runStateLabel("waiting")).toBe("Waiting for you");
    expect(runStateLabel("failed")).toBe("The last run failed");
    expect(runStateLabel("done")).toBeNull();
    expect(runStateLabel(undefined)).toBeNull();
  });

  it("knows when a session is open", () => {
    expect(anyOpen([{ run_state: "done" }, { run_state: "waiting" }])).toBe(true);
    expect(anyOpen([{ run_state: null }, {}])).toBe(false);
  });

  it("filters by title", () => {
    const sessions = [
      { id: "abcdef1234", title: "Fix login" },
      { id: "0123456789", title: null },
    ];
    expect(filterSessions(sessions, "LOGIN").map((s) => s.id)).toEqual(["abcdef1234"]);
    expect(filterSessions(sessions, "session 0123").map((s) => s.id)).toEqual(["0123456789"]);
    expect(filterSessions(sessions, " ")).toHaveLength(2);
  });
});
