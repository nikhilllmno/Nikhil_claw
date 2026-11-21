// The role permission editor of the settings console (docs/architecture.md, Section 17.7,
// M7.10): a table of permissions (rows) and roles (columns). The owner always has every
// permission. The API checks every change again; these helpers only choose what the screen shows
// and what it sends.

import type { EditableRole, PermissionInfo, PermissionKey, RolePermissions } from "./api";

export const EDITABLE_ROLES: readonly EditableRole[] = ["admin", "member"];

export const EDITABLE_ROLE_LABELS: Record<EditableRole, string> = {
  admin: "Admin",
  member: "Member",
};

/** The choices of one role that the user changed and did not save. A key is here only when it differs from the saved value. */
export type PermissionDraft = Partial<Record<PermissionKey, boolean>>;

/** True when the permission is for the role. A cell of another role shows a dash. */
export function appliesTo(info: Pick<PermissionInfo, "roles">, role: EditableRole): boolean {
  return info.roles.includes(role);
}

/** The saved value of a cell. A key with no value is allowed: the default of every key is yes. */
export function savedValue(data: RolePermissions, role: EditableRole, key: PermissionKey): boolean {
  return data.roles[role]?.[key] ?? true;
}

/** The value that the cell shows: the draft, else the saved value. */
export function cellValue(
  data: RolePermissions,
  role: EditableRole,
  key: PermissionKey,
  draft: PermissionDraft,
): boolean {
  return draft[key] ?? savedValue(data, role, key);
}

/** The draft after a click. A cell that goes back to its saved value leaves the draft. */
export function toggleDraft(
  data: RolePermissions,
  role: EditableRole,
  draft: PermissionDraft,
  key: PermissionKey,
  value: boolean,
): PermissionDraft {
  const next = { ...draft };
  if (value === savedValue(data, role, key)) delete next[key];
  else next[key] = value;
  return next;
}

/** The body of a save: only the keys that changed, and only keys that are for the role. */
export function changedPermissions(
  data: RolePermissions,
  role: EditableRole,
  draft: PermissionDraft,
): PermissionDraft {
  const out: PermissionDraft = {};
  for (const info of data.permissions) {
    if (!appliesTo(info, role)) continue;
    const value = draft[info.key];
    if (value !== undefined && value !== savedValue(data, role, info.key)) out[info.key] = value;
  }
  return out;
}

export function changedCount(data: RolePermissions, role: EditableRole, draft: PermissionDraft): number {
  return Object.keys(changedPermissions(data, role, draft)).length;
}

/** The labels of the permissions that a role loses with a draft, for the confirm text. */
export function revokedLabels(data: RolePermissions, role: EditableRole, draft: PermissionDraft): string[] {
  const changed = changedPermissions(data, role, draft);
  return data.permissions.filter((p) => changed[p.key] === false).map((p) => p.label);
}

/** One line that says who can change the table. */
export function permissionNote(editable: Record<EditableRole, boolean>): string {
  if (editable.admin && editable.member) return "As the org owner, you can change both columns.";
  if (editable.member) return "As an admin, you can change the Member column. Only the org owner can change the Admin column.";
  return "Only an org owner can change the Admin column. An owner or an admin can change the Member column.";
}

/** The accessible name of a checkbox of the table. */
export function cellLabel(info: Pick<PermissionInfo, "label">, role: EditableRole): string {
  return `${info.label}: ${EDITABLE_ROLE_LABELS[role]}`;
}
