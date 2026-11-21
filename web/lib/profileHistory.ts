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

/** One line for the head of a change: what changed, how, and who did it. */
export function changeTitle(change: ProfileChange): string {
  const who = change.actor_name ? ` by ${change.actor_name}` : "";
  return `${KIND_LABELS[change.kind]} · ${SOURCE_LABELS[change.source]}${who}`;
}

