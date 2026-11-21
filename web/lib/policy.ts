// Helpers for the tool policy in the Tools section of the console: the draft of the rules and
// its change to the body of a save. The rules mirror src/sama/profile_changes.py; the API checks
// them again.

import type { PolicyAction, ToolInfo, ToolPolicyChange, ToolPolicyRule } from "./api";

export const TOOL_MAX_CHARS = 200;

/** `default`: the profile has no rule for the tool. */
export type PolicyChoice = PolicyAction | "default";

export const POLICY_CHOICES: readonly PolicyChoice[] = ["default", "allow", "ask", "deny"];

export function policyActionLabel(action: PolicyAction): string {
  switch (action) {
    case "allow":
      return "Allow";
    case "ask":
      return "Ask";
    case "deny":
      return "Deny";
  }
}

export function policyChoiceLabel(choice: PolicyChoice): string {
  return choice === "default" ? "Default" : policyActionLabel(choice);
}

/** A rule that is for a prefix of tool names (it ends with `*`). */
export function isPrefixRule(tool: string): boolean {
  return tool.endsWith("*");
}

/** The rules of `rules` as `{ tool: action }`. */
export function ruleMap(rules: readonly ToolPolicyRule[]): Record<string, PolicyAction> {
  const map: Record<string, PolicyAction> = {};
  for (const rule of rules) map[rule.tool] = rule.action;
  return map;
}

/** The rules that no listed tool owns: a prefix rule, or a rule for a tool that no toolset has. */
export function customRules(rules: readonly ToolPolicyRule[], tools: readonly ToolInfo[]): ToolPolicyRule[] {
  const names = new Set(tools.map((t) => t.name));
  return rules.filter((rule) => !names.has(rule.tool));
}

/** The tools grouped by toolset, in the order of the list. A group has the state of its first tool. */
export function groupTools(tools: readonly ToolInfo[]): { toolset: string; enabled: boolean; tools: ToolInfo[] }[] {
  const groups: { toolset: string; enabled: boolean; tools: ToolInfo[] }[] = [];
  for (const tool of tools) {
    let group = groups.find((g) => g.toolset === tool.toolset);
    if (!group) {
      group = { toolset: tool.toolset, enabled: tool.enabled, tools: [] };
      groups.push(group);
    }
    group.tools.push(tool);
  }
  return groups;
}

/**
 * The rule of the org that applies to `tool`: the rule for the exact name, else the rule of the
 * longest prefix. `null`: no org rule.
 */
export function orgRuleFor(
  tool: string,
  orgRules: readonly ToolPolicyRule[],
): { action: PolicyAction; via: string } | null {
  const exact = orgRules.find((r) => r.tool === tool);
  if (exact) return { action: exact.action, via: exact.tool };
  let best: ToolPolicyRule | null = null;
  for (const rule of orgRules) {
    if (!isPrefixRule(rule.tool)) continue;
    if (!tool.startsWith(rule.tool.slice(0, -1))) continue;
    if (best === null || rule.tool.length > best.tool.length) best = rule;
  }
  return best ? { action: best.action, via: best.tool } : null;
}

/**
 * The changes that turn the `saved` rules into the `wanted` rules, sorted by tool. A saved rule
 * that `wanted` does not have is removed (`action: null`).
 */
export function policyChanges(
  saved: readonly ToolPolicyRule[],
  wanted: Readonly<Record<string, PolicyAction>>,
): ToolPolicyChange[] {
  const before = ruleMap(saved);
  const changes: ToolPolicyChange[] = [];
  for (const tool of new Set([...Object.keys(before), ...Object.keys(wanted)])) {
    const action = wanted[tool] ?? null;
    if (action !== (before[tool] ?? null)) changes.push({ tool, action });
  }
  return changes.sort((a, b) => (a.tool < b.tool ? -1 : a.tool > b.tool ? 1 : 0));
}

/** A custom rule row of the form. */
export interface RuleRow {
  /** A key for React. It is not sent. */
  uid: number;
  tool: string;
  action: PolicyAction;
}

let nextUid = 1;

export function newRuleRow(tool = "", action: PolicyAction = "ask"): RuleRow {
  return { uid: nextUid++, tool, action };
}

export function toRuleRows(rules: readonly ToolPolicyRule[]): RuleRow[] {
  return rules.map((r) => newRuleRow(r.tool, r.action));
}

/** The first problem of a tool name of a rule, or `null`. */
export function validateRuleTool(raw: string): string | null {
  const tool = raw.trim();
  if (tool === "") return "Each rule needs a tool name or a prefix.";
  if (tool.length > TOOL_MAX_CHARS) return `A tool name has at most ${TOOL_MAX_CHARS} characters.`;
  if (/\s/.test(tool) || tool.slice(0, -1).includes("*")) {
    return `The tool ${tool} is not valid. Use a name, or a prefix with * last.`;
  }
  return null;
}

/**
 * The first problem of the rows of custom rules, or `null`. `taken` holds the names that the
 * tool table already has, so a row cannot repeat them.
 */
export function validateRuleRows(rows: readonly RuleRow[], taken: ReadonlySet<string> = new Set()): string | null {
  const seen = new Set<string>();
  for (const row of rows) {
    const problem = validateRuleTool(row.tool);
    if (problem) return problem;
    const tool = row.tool.trim();
    if (taken.has(tool)) return `The tool ${tool} is in the table. Set its rule there.`;
    if (seen.has(tool)) return `Two rules have the tool ${tool}.`;
    seen.add(tool);
  }
  return null;
}

/** The rules that the table and the custom rows mean together. */
export function wantedRules(
  table: Readonly<Record<string, PolicyChoice>>,
  rows: readonly RuleRow[],
): Record<string, PolicyAction> {
  const wanted: Record<string, PolicyAction> = {};
  for (const [tool, choice] of Object.entries(table)) {
    if (choice !== "default") wanted[tool] = choice;
  }
  for (const row of rows) wanted[row.tool.trim()] = row.action;
  return wanted;
}

/** The table draft of the tools with a saved rule. */
export function tableDraft(rules: readonly ToolPolicyRule[], tools: readonly ToolInfo[]): Record<string, PolicyChoice> {
  const names = new Set(tools.map((t) => t.name));
  const draft: Record<string, PolicyChoice> = {};
  for (const rule of rules) if (names.has(rule.tool)) draft[rule.tool] = rule.action;
  return draft;
}

/** A problem when a save has more changes than the API takes at once, else `null`. */
export function checkChangeCount(changes: readonly ToolPolicyChange[], maxRules: number): string | null {
  if (changes.length <= maxRules) return null;
  return `Change at most ${maxRules} rules at a time. You changed ${changes.length}. Save, then change the rest.`;
}
