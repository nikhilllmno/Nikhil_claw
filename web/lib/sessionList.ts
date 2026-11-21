// The Sessions section of the settings console (docs/architecture.md, Section 17.7, M7.10): the
// sessions of one workspace, an Active and an Archived list, and the rename and archive rules
// that the screen shows. The API checks every change again.

import type { SessionOut } from "./api";

export type SessionFilter = "active" | "archived";

export const SESSION_FILTERS: readonly { id: SessionFilter; label: string }[] = [
  { id: "active", label: "Active" },
  { id: "archived", label: "Archived" },
];

/** The most characters of a title (the API gives 200). */
export const MAX_TITLE_CHARS = 200;

/** The most sessions that the list asks for. */
export const LIST_LIMIT = 100;

/**
 * The sessions of one workspace. The archive of a compacted transcript is not a session to open,
 * so it stays out.
 */
export function sessionsOfWorkspace(sessions: readonly SessionOut[], workspaceId: string | null): SessionOut[] {
  if (workspaceId === null) return [];
  return sessions.filter((s) => s.workspace_id === workspaceId && s.lineage !== "compaction");
}

/** The title that the user typed, trimmed, or an error text. */
export function checkTitle(value: string): { title: string } | { error: string } {
  const title = value.trim();
  if (title === "") return { error: "Give the session a title." };
  if (title.length > MAX_TITLE_CHARS) return { error: `Use ${MAX_TITLE_CHARS} characters or fewer.` };
  return { title };
}

/** True when the title of the draft is a change that the screen can save. */
export function titleChanged(current: string | null, draft: string): boolean {
  const next = draft.trim();
  return next !== "" && next !== (current ?? "").trim();
}

/** The list without the session. */
export function withoutSession(sessions: readonly SessionOut[], id: string): SessionOut[] {
  return sessions.filter((s) => s.id !== id);
}

/** The list with the changed session in place of the old one. */
export function replaceSession(sessions: readonly SessionOut[], changed: SessionOut): SessionOut[] {
  return sessions.map((s) => (s.id === changed.id ? changed : s));
}

export function archiveConfirmText(title: string): string {
  return `Archive ${title}? It leaves the list of active sessions. You can restore it later.`;
}

/** The line of an empty list. */
export function emptyText(filter: SessionFilter, workspaceName: string): string {
  return filter === "active"
    ? `${workspaceName} has no active sessions.`
    : `${workspaceName} has no archived sessions.`;
}

/** True when the list can have more sessions than it shows. */
export function maybeTruncated(total: number): boolean {
  return total >= LIST_LIMIT;
}

/**
 * True when the screen offers rename, archive, and restore for the session. An API without
 * `can_manage` (an old API) gives `true`, and the API still checks each change.
 */
export function canManageSession(session: Pick<SessionOut, "can_manage">): boolean {
  return session.can_manage !== false;
}
