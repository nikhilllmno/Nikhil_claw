// Labels and rules for the change history of an agent profile (M6.1).

import type { ProfileChange, ProfileChangeKind, ProfileChangeSource } from "./api";

const KIND_LABELS: Record<ProfileChangeKind, string> = {
  soul: "Soul",
  profile: "Settings",
  toolsets: "Tools",
  policy: "Policy rules",
};

const SOURCE_LABELS: Record<ProfileChangeSource, string> = {
  proposal: "Approved proposal",
  edit: "Edit",
  revert: "Revert",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind as ProfileChangeKind] ?? kind;
}

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source as ProfileChangeSource] ?? source;
}

/** One line for the head of a change: what changed, how, and who did it. */
export function changeTitle(change: ProfileChange): string {
  const who = change.actor_name ? ` by ${change.actor_name}` : "";
  return `${kindLabel(change.kind)} · ${sourceLabel(change.source)}${who}`;
}

/**
 * True when the user can revert the change: the user can edit the agent, and the change is not
 * reverted. The API also refuses a change when the agent changed after it (409).
 */
export function canRevert(change: ProfileChange, editable: boolean): boolean {
  return editable && change.reverted_by_change_id === null;
}
