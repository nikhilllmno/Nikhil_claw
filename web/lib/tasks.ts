// The logic of the task board screen (docs/architecture.md, Section 12.4). The screen shows
// one column for each open status and one for done. A cancelled card is hidden.

import type { AgentProfileSummary, Audience, NewTaskBody, TaskOut, TaskPatch, TaskStatus } from "./api";

export const COLUMNS: readonly { status: TaskStatus; title: string; empty: string }[] = [
  { status: "todo", title: "To do", empty: "No cards to do." },
  { status: "running", title: "Running", empty: "No agent works on a card now." },
  { status: "blocked", title: "Blocked", empty: "No blocked cards." },
  { status: "done", title: "Done", empty: "No done cards yet." },
];

export const MAX_TITLE = 200;
export const MAX_BODY = 20_000;
export const MAX_COMMENT = 4_000;

/** The cards of each column, newest first. */
export function columnsOf(tasks: readonly TaskOut[]): Map<TaskStatus, TaskOut[]> {
  const out = new Map<TaskStatus, TaskOut[]>(COLUMNS.map((c) => [c.status, []]));
  for (const task of tasks) out.get(task.status)?.push(task);
  for (const list of out.values()) list.sort((a, b) => b.created_at.localeCompare(a.created_at));
  return out;
}

/** The statuses that a person can set. Only the dispatcher sets `running`. */
export function statusChoices(task: TaskOut): TaskStatus[] {
  const all: TaskStatus[] = ["todo", "blocked", "done", "cancelled"];
  return task.status === "running" ? ["running", ...all] : all;
}

export const STATUS_LABEL: Record<TaskStatus, string> = {
  todo: "To do",
  running: "Running",
  blocked: "Blocked",
  done: "Done",
  cancelled: "Cancelled",
};

/** Who does the card: an agent name, "You", "A person", or "No one". */
export function assigneeLabel(
  task: TaskOut,
  profiles: readonly AgentProfileSummary[],
  userId: string | null,
): string {
  if (task.assignee_profile_id !== null) {
    const profile = profiles.find((p) => p.id === task.assignee_profile_id);
    return profile ? `Agent: ${profile.name}` : "Agent";
  }
  if (task.assignee_user_id !== null) {
    return task.assignee_user_id === userId ? "You" : "A person";
  }
  return "No one";
}

/**
 * The value of the assignee select: `agent:<profile id>`, `me`, `person` (someone else), or
 * `none`.
 */
export function assigneeValue(task: TaskOut, userId: string | null): string {
  if (task.assignee_profile_id !== null) return `agent:${task.assignee_profile_id}`;
  if (task.assignee_user_id !== null) return task.assignee_user_id === userId ? "me" : "person";
  return "none";
}

/** The change for a new value of the assignee select. `null` for no change. */
export function assigneePatch(value: string, userId: string | null): TaskPatch | null {
  if (value === "none") return { assignee: "none" };
  if (value === "me") return userId === null ? null : { assignee: "person", assignee_user_id: userId };
  if (value.startsWith("agent:")) {
    return { assignee: "agent", assignee_profile_id: value.slice("agent:".length) };
  }
  return null;
}

export interface NewTaskDraft {
  title: string;
  body: string;
  audience: Audience;
  /** The value of the assignee select. */
  assignee: string;
}

export const EMPTY_DRAFT: NewTaskDraft = { title: "", body: "", audience: "shared", assignee: "none" };

/** The problem of a draft, or `null`. */
export function validateNewTask(draft: NewTaskDraft): string | null {
  const title = draft.title.trim();
  if (title === "") return "Give the card a title.";
  if (title.length > MAX_TITLE) return `Use ${MAX_TITLE} characters or fewer for the title.`;
  if (draft.body.length > MAX_BODY) return `Use ${MAX_BODY} characters or fewer for the details.`;
  return null;
}

/** The body of `POST /workspaces/{id}/tasks` for a valid draft. */
export function newTaskBody(draft: NewTaskDraft, userId: string | null): NewTaskBody {
  const agent = draft.assignee.startsWith("agent:") ? draft.assignee.slice("agent:".length) : null;
  return {
    title: draft.title.trim(),
    body: draft.body.trim(),
    audience: draft.audience,
    assignee_profile_id: agent,
    assignee_user_id: draft.assignee === "me" ? userId : null,
  };
}

/** Put a changed card into the list, or add a new one. */
export function upsertTask(tasks: readonly TaskOut[], task: TaskOut): TaskOut[] {
  const index = tasks.findIndex((t) => t.id === task.id);
  if (index === -1) return [task, ...tasks];
  const out = tasks.slice();
  out[index] = task;
  return out;
}

/** True if the board has a card that an agent works on, so the screen loads again soon. */
export function hasLiveCards(tasks: readonly TaskOut[]): boolean {
  return tasks.some(
    (t) => t.status === "running" || (t.status === "todo" && t.assignee_profile_id !== null),
  );
}
