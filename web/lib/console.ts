// The settings console (docs/architecture.md, Section 17.7, M7.10): the sections, their groups,
// and the permission checks that the screens use. The screens are in components/console/.

import type { Org, PermissionKey } from "./api";

export type SectionId =
  | "setup"
  | "slack"
  | "roles"
  | "api-keys"
  | "harness"
  | "tools"
  | "subagents"
  | "skills"
  | "mcp"
  | "webhooks"
  | "triggers"
  | "schedules"
  | "workflows"
  | "sessions";

export type SectionGroup = "Org" | "Agents" | "Automation" | "Activity";

export interface ConsoleSection {
  id: SectionId;
  title: string;
  /** One line about the section, in the navigation tooltip and under the title. */
  description: string;
  group: SectionGroup;
}

/** The sections in the order of the navigation. */
export const SECTIONS: readonly ConsoleSection[] = [
  { id: "setup", title: "Setup", group: "Org", description: "The steps that make the org ready to use." },
  { id: "slack", title: "Slack", group: "Org", description: "The Slack app, the other chat apps, and the linked chat accounts." },
  { id: "roles", title: "Roles", group: "Org", description: "The members, their roles, and what each role can do." },
  { id: "api-keys", title: "API keys", group: "Org", description: "The keys that let other apps use sama through the MCP server and the OpenAI-compatible endpoint." },
  { id: "harness", title: "Harness", group: "Agents", description: "The runtime, the model, the reasoning effort, the busy mode, and the budget of each agent." },
  { id: "tools", title: "Tools", group: "Agents", description: "The toolsets of each agent and the policy of each tool." },
  { id: "subagents", title: "Sub-agents", group: "Agents", description: "The sub-agents that an agent can start." },
  { id: "skills", title: "Skills", group: "Agents", description: "The guides that the agents follow." },
  { id: "mcp", title: "MCP servers", group: "Agents", description: "The MCP servers that give the agents more tools." },
  { id: "webhooks", title: "Webhooks", group: "Automation", description: "Signed webhooks that start a run or a workflow." },
  { id: "triggers", title: "Triggers", group: "Automation", description: "Events of connected apps that start a run or a workflow." },
  { id: "schedules", title: "Schedules", group: "Automation", description: "Jobs that run the agent on a schedule." },
  { id: "workflows", title: "Workflows", group: "Automation", description: "Durable workflows of steps, approvals, and waits." },
  { id: "sessions", title: "Sessions", group: "Activity", description: "Find, rename, archive, and keep sessions." },
];

export const SECTION_GROUPS: readonly SectionGroup[] = ["Org", "Agents", "Automation", "Activity"];

export const DEFAULT_SECTION: SectionId = "setup";

export function sectionById(id: string | undefined | null): ConsoleSection | null {
  return SECTIONS.find((s) => s.id === id) ?? null;
}

/** The sections of each group, in the order of the navigation. Groups without sections are left out. */
export function groupedSections(): { group: SectionGroup; sections: ConsoleSection[] }[] {
  return SECTION_GROUPS.map((group) => ({
    group,
    sections: SECTIONS.filter((s) => s.group === group),
  })).filter((g) => g.sections.length > 0);
}

/**
 * True when the role of the user in the org has the permission. An API without the role
 * permissions (no `permissions` on the org) gives `true`, and the API still checks each write.
 */
export function can(org: Org, key: PermissionKey): boolean {
  if (org.role === "owner") return true;
  if (org.permissions === undefined) return true;
  return org.permissions.includes(key);
}

/** True when the user can change the settings of the agents (an org admin with `agents.manage`). */
export function canManageAgents(org: Org): boolean {
  return org.admin && can(org, "agents.manage");
}
