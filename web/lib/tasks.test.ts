import { describe, expect, it } from "vitest";
import type { AgentProfileSummary, TaskOut } from "./api";
import {
  EMPTY_DRAFT,
  assigneeLabel,
  assigneePatch,
  assigneeValue,
  columnsOf,
  hasLiveCards,
  newTaskBody,
  statusChoices,
  upsertTask,
  validateNewTask,
} from "./tasks";

function task(changes: Partial<TaskOut> = {}): TaskOut {
  return {
    id: "t1",
    workspace_id: "w",
    title: "Write the report",
    body: "",
    status: "todo",
    audience: "shared",
    owner_user_id: null,
    assignee_profile_id: null,
    assignee_user_id: null,
    parent_task_id: null,
    depends_on: [],
    session_id: null,
    run_id: null,
    result: null,
    created_by_user_id: "u1",
    created_at: "2026-11-20T10:00:00Z",
    updated_at: "2026-11-20T10:00:00Z",
    finished_at: null,
    ...changes,
  };
}

const profiles: AgentProfileSummary[] = [
  { id: "p1", name: "sama", workspace_id: null, runtime: "claude_agent_sdk", model: "m" },
];

describe("columnsOf", () => {
  it("puts each card in its column, newest first, and hides cancelled cards", () => {
    const columns = columnsOf([
      task({ id: "a", created_at: "2026-11-20T09:00:00Z" }),
      task({ id: "b", created_at: "2026-11-20T11:00:00Z" }),
      task({ id: "c", status: "done" }),
      task({ id: "d", status: "cancelled" }),
    ]);
    expect(columns.get("todo")?.map((t) => t.id)).toEqual(["b", "a"]);
    expect(columns.get("done")?.map((t) => t.id)).toEqual(["c"]);
    expect(columns.get("running")).toEqual([]);
    expect(columns.has("cancelled")).toBe(false);
  });
});

describe("statusChoices", () => {
  it("never offers running, except as the current status", () => {
    expect(statusChoices(task())).toEqual(["todo", "blocked", "done", "cancelled"]);
    expect(statusChoices(task({ status: "running" }))[0]).toBe("running");
  });
});

describe("assignee", () => {
  it("names the assignee", () => {
    expect(assigneeLabel(task({ assignee_profile_id: "p1" }), profiles, "u1")).toBe("Agent: sama");
    expect(assigneeLabel(task({ assignee_profile_id: "p9" }), profiles, "u1")).toBe("Agent");
    expect(assigneeLabel(task({ assignee_user_id: "u1" }), profiles, "u1")).toBe("You");
    expect(assigneeLabel(task({ assignee_user_id: "u2" }), profiles, "u1")).toBe("A person");
    expect(assigneeLabel(task(), profiles, "u1")).toBe("No one");
  });

  it("maps the select value both ways", () => {
    expect(assigneeValue(task({ assignee_profile_id: "p1" }), "u1")).toBe("agent:p1");
    expect(assigneeValue(task({ assignee_user_id: "u1" }), "u1")).toBe("me");
    expect(assigneeValue(task({ assignee_user_id: "u2" }), "u1")).toBe("person");
    expect(assigneePatch("agent:p1", "u1")).toEqual({ assignee: "agent", assignee_profile_id: "p1" });
    expect(assigneePatch("me", "u1")).toEqual({ assignee: "person", assignee_user_id: "u1" });
    expect(assigneePatch("me", null)).toBeNull();
    expect(assigneePatch("none", "u1")).toEqual({ assignee: "none" });
    expect(assigneePatch("person", "u1")).toBeNull();
  });
});

describe("new card", () => {
  it("needs a title of 200 characters or fewer", () => {
    expect(validateNewTask(EMPTY_DRAFT)).toMatch(/title/);
    expect(validateNewTask({ ...EMPTY_DRAFT, title: "x".repeat(201) })).toMatch(/200/);
    expect(validateNewTask({ ...EMPTY_DRAFT, title: " Plan " })).toBeNull();
  });

  it("builds the request body", () => {
    expect(newTaskBody({ ...EMPTY_DRAFT, title: " Plan ", assignee: "agent:p1" }, "u1")).toEqual({
      title: "Plan",
      body: "",
      audience: "shared",
      assignee_profile_id: "p1",
      assignee_user_id: null,
    });
    expect(
      newTaskBody({ ...EMPTY_DRAFT, title: "Plan", audience: "private", assignee: "me" }, "u1"),
    ).toMatchObject({ audience: "private", assignee_profile_id: null, assignee_user_id: "u1" });
  });
});

describe("list updates", () => {
  it("replaces a changed card and adds a new one first", () => {
    const list = [task({ id: "a" }), task({ id: "b" })];
    expect(upsertTask(list, task({ id: "b", status: "done" }))[1]?.status).toBe("done");
    expect(upsertTask(list, task({ id: "c" })).map((t) => t.id)).toEqual(["c", "a", "b"]);
  });

  it("knows when an agent works on the board", () => {
    expect(hasLiveCards([task()])).toBe(false);
    expect(hasLiveCards([task({ assignee_profile_id: "p1" })])).toBe(true);
    expect(hasLiveCards([task({ status: "running" })])).toBe(true);
  });
});
