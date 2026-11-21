// The admin views of the web app (docs/architecture.md, Section 17.6, M6.9): usage, audit log,
// data controls, curator reports, and the change history of the agents. The API checks every
// rule again; these helpers only shape what the screens show.

import type { AuditEntry, DataSettings, ProfileChange, UsageDay, UsageRow } from "./api";

/** The windows of the usage page, in days. The API takes 1 to 90. */
export const USAGE_RANGES = [7, 30, 90] as const;
export type UsageRange = (typeof USAGE_RANGES)[number];

function amount(usd: string): number {
  const value = Number.parseFloat(usd);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

/** The bars of the daily cost chart: each day with its share of the most expensive day (0-100). */
export function dayBars(days: UsageDay[]): { day: string; cost: number; percent: number }[] {
  const costs = days.map((d) => amount(d.cost_usd));
  const max = Math.max(0, ...costs);
  return days.map((d, i) => ({
    day: d.day,
    cost: costs[i] ?? 0,
    percent: max === 0 ? 0 : Math.round(((costs[i] ?? 0) / max) * 100),
  }));
}

/** The share of a breakdown row in the total cost, as a whole percent (0-100). */
export function costShare(row: Pick<UsageRow, "cost_usd">, totalUsd: string): number {
  const total = amount(totalUsd);
  if (total === 0) return 0;
  return Math.min(100, Math.round((amount(row.cost_usd) / total) * 100));
}

/** A UTC day (`YYYY-MM-DD`) as a short date, for example "21 Nov". */
export function shortDay(day: string): string {
  const date = new Date(`${day}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return day;
  return date.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
}

/** The filters of the audit log: a prefix of the action, and its label. */
export const AUDIT_FILTERS = [
  { value: "", label: "All events" },
  { value: "member.", label: "Members" },
  { value: "invite.", label: "Invites" },
  { value: "profile.", label: "Agents" },
  { value: "vault.", label: "Keys and secrets" },
  { value: "integration.", label: "Chat apps" },
  { value: "data.", label: "Data controls" },
  { value: "skill.", label: "Skills" },
  { value: "job.", label: "Jobs" },
  { value: "session.", label: "Sessions" },
  { value: "org.", label: "Org" },
] as const;

const AUDIT_LABELS: Record<string, string> = {
  "org.created": "Created the org",
  "org.region_changed": "Changed the region",
  "invite.created": "Sent an invite",
  "invite.revoked": "Revoked an invite",
  "invite.accepted": "Accepted an invite",
  "member.role_changed": "Changed a role",
  "member.workspaces_changed": "Changed the workspaces of a member",
  "member.removed": "Removed a member",
  "member.left": "Left the org",
  "profile.created": "Created an agent",
  "profile.updated": "Changed an agent",
  "vault.secret_set": "Set a secret",
  "vault.secret_deleted": "Removed a secret",
  "integration.created": "Started a chat app",
  "data.exported": "Exported data",
  "data.retention_changed": "Changed the retention",
  "data.retention": "Deleted old sessions",
  "user.deleted": "Deleted a user",
  "session.created": "Started a session",
  "skill.stale": "Marked a skill as stale",
  "skill.active": "Marked a skill as active",
  "skill.archive": "Archived a skill",
  "skill.pin": "Pinned a skill",
  "skill.unpin": "Unpinned a skill",
  "skill.restore": "Restored a skill",
  "job.created": "Created a job",
};

/** A readable label for an audit action. An unknown action shows as words. */
export function auditLabel(action: string): string {
  const known = AUDIT_LABELS[action];
  if (known) return known;
  const words = action.replace(/[._]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : action;
}

/** Who did it: the name of the user, or the kind of actor. */
export function auditActor(entry: Pick<AuditEntry, "actor_kind" | "actor_name" | "actor_user_id">): string {
  if (entry.actor_name) return entry.actor_name;
  if (entry.actor_kind === "system") return "Sama";
  if (entry.actor_kind === "agent") return "An agent";
  return entry.actor_user_id ? "A removed user" : "Unknown";
}

function shortValue(value: unknown): string | null {
  if (value === null || value === undefined) return "none";
  if (typeof value === "string") return value.length > 60 ? `${value.slice(0, 57)}…` : value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value) && value.every((v) => typeof v === "string" || typeof v === "number")) {
    const text = value.join(", ");
    return text.length > 60 ? `${text.slice(0, 57)}…` : text || "none";
  }
  return null;
}

/** At most `max` short `key: value` parts of the details. Nested objects are left out. */
export function auditDetails(details: Record<string, unknown>, max = 4): string[] {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(details)) {
    const text = shortValue(value);
    if (text === null) continue;
    parts.push(`${key.replace(/_/g, " ")}: ${text}`);
    if (parts.length >= max) break;
  }
  return parts;
}

/** The choices of the retention setting. `null`: the default of the plan. */
export const RETENTION_CHOICES: (number | null)[] = [null, 30, 90, 180, 365, 730];

export function daysText(days: number | null): string {
  if (days === null) return "Keep everything";
  if (days % 365 === 0) return days === 365 ? "1 year" : `${days / 365} years`;
  return days === 1 ? "1 day" : `${days} days`;
}

/** The label of a retention choice on the select. */
export function retentionChoice(days: number | null, settings: Pick<DataSettings, "plan_default_days">): string {
  if (days === null) return `Plan default (${daysText(settings.plan_default_days).toLowerCase()})`;
  return daysText(days);
}

/** The choices that the plan allows. A choice over the plan maximum is not offered. */
export function retentionChoices(settings: Pick<DataSettings, "plan_max_days" | "retention_days">): (number | null)[] {
  const max = settings.plan_max_days;
  const choices = RETENTION_CHOICES.filter((d) => d === null || max === null || d <= max);
  // Keep the current value, also when it is not a standard choice.
  if (settings.retention_days !== null && !choices.includes(settings.retention_days)) {
    choices.push(settings.retention_days);
    choices.sort((a, b) => (a ?? 0) - (b ?? 0));
  }
  return choices;
}

/** One line about the retention in use. */
export function retentionSummary(settings: DataSettings): string {
  if (settings.effective_days === null) {
    return "Sama keeps every session until someone deletes it.";
  }
  return `Sama deletes a session after ${daysText(settings.effective_days)} without activity.`;
}

/** True when the typed text is the slug of the org. A delete of the org needs it. */
export function confirmsSlug(typed: string, slug: string): boolean {
  return typed.trim() === slug && slug !== "";
}

/** Saves a file that the API sent (an export) with a temporary link. */
export function saveFile(name: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** A change of one agent, with the agent. */
export interface AgentChange extends ProfileChange {
  profile_id: string;
  profile_name: string;
}

/** The changes of all the agents, newest first, at most `limit`. */
export function mergeChanges(
  lists: { profileId: string; profileName: string; changes: ProfileChange[] }[],
  limit = 100,
): AgentChange[] {
  const all = lists.flatMap((list) =>
    list.changes.map((change) => ({ ...change, profile_id: list.profileId, profile_name: list.profileName })),
  );
  all.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.id.localeCompare(a.id));
  return all.slice(0, limit);
}

/** The first line of a report, for the list. */
export function reportTitle(output: string | null, fallback: string): string {
  const first = (output ?? "")
    .split("\n")
    .map((line) => line.replace(/^#+\s*/, "").trim())
    .find((line) => line !== "");
  return first ?? fallback;
}
