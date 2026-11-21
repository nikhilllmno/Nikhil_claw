// The sub-agent library (docs/architecture.md, roadmap M7.4): the types of the API, and the
// helpers of the library screen and its editor. The API checks every write again. The checks
// here only give the user a fast answer.

// -- types --------------------------------------------------------------------------------------

export type SubagentScope = "user" | "workspace" | "org";

export interface LibrarySpec {
  name: string;
  description: string;
  prompt: string;
  toolsets: string[];
  /** `null`: the model of the run. */
  model: string | null;
  max_turns: number | null;
  /** A decimal string, for example `0.50`. */
  max_cost_usd: string | null;
}

export interface LibraryAgent extends LibrarySpec {
  id: string;
  scope: SubagentScope;
  workspace_id: string | null;
  user_id: string | null;
  version: number;
  updated_at: string;
  can_edit: boolean;
  /** The agent profiles that use the agent. */
  used_by: { id: string; name: string }[];
}

export interface LibraryOptions {
  toolsets: { name: string; tools: string[] }[];
  models: { runtime: string; models: string[] }[];
  limits: {
    name_max_chars: number;
    name_pattern: string;
    description_max_chars: number;
    prompt_max_chars: number;
    max_turns: number;
    max_cost_usd: string;
  };
}

export interface SubagentCatalog {
  agents: LibraryAgent[];
  writable_scopes: SubagentScope[];
  options: LibraryOptions;
}

export interface LibraryVersion {
  version: number;
  source: "create" | "edit" | "revert";
  reverts_version: number | null;
  author_user_id: string | null;
  created_at: string;
  spec: LibrarySpec;
}

/** The body of `POST /orgs/{org_id}/subagents`. */
export type LibraryCreate = LibrarySpec & { scope: SubagentScope; workspace_id?: string };

/** The body of `PATCH /orgs/{org_id}/subagents/{agent_id}`. Each field is optional. */
export type LibraryPatch = Partial<LibrarySpec>;

export interface LibraryTestBody {
  workspace_id: string;
  profile_id: string;
  message: string;
}

export interface LibraryTestResult {
  session_id: string;
  run_id: string;
}

// -- labels -------------------------------------------------------------------------------------

export const SCOPE_LABELS: Record<SubagentScope, string> = {
  user: "Personal",
  workspace: "Workspace",
  org: "Org",
};

export const SCOPE_HINTS: Record<SubagentScope, string> = {
  user: "Only you can use and change it.",
  workspace: "Agents of this workspace can use it. A workspace admin or an org owner changes it.",
  org: "Agents of every workspace can use it. An org owner or admin changes it.",
};

export const SCOPE_ORDER: readonly SubagentScope[] = ["user", "workspace", "org"];

export const SOURCE_LABELS: Record<LibraryVersion["source"], string> = {
  create: "Created",
  edit: "Edited",
  revert: "Reverted",
};

/** The text of a version row, for example `Reverted to version 2`. */
export function versionSource(version: Pick<LibraryVersion, "source" | "reverts_version">): string {
  const label = SOURCE_LABELS[version.source];
  return version.source === "revert" && version.reverts_version !== null
    ? `${label} to version ${version.reverts_version}`
    : label;
}

// -- limits -------------------------------------------------------------------------------------

export const DEFAULT_LIMITS: LibraryOptions["limits"] = {
  name_max_chars: 40,
  name_pattern: "^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
  description_max_chars: 300,
  prompt_max_chars: 8000,
  max_turns: 50,
  max_cost_usd: "10",
};

export const MAX_TEST_MESSAGE_CHARS = 4000;

function safeRegExp(source: string): RegExp | null {
  try {
    return new RegExp(source);
  } catch {
    return null;
  }
}

const DECIMAL = /^\d+(?:\.\d+)?$/;

// -- draft --------------------------------------------------------------------------------------

/** The form of the editor. The numbers are the text of their fields. */
export interface SubagentDraft {
  name: string;
  description: string;
  prompt: string;
  toolsets: string[];
  /** Empty: the model of the run. */
  model: string;
  /** Empty: the default. */
  maxTurns: string;
  /** Empty: no limit. */
  maxCost: string;
}

export function emptyDraft(): SubagentDraft {
  return { name: "", description: "", prompt: "", toolsets: [], model: "", maxTurns: "", maxCost: "" };
}

export function toDraft(agent: LibrarySpec): SubagentDraft {
  return {
    name: agent.name,
    description: agent.description,
    prompt: agent.prompt,
    toolsets: [...agent.toolsets],
    model: agent.model ?? "",
    maxTurns: agent.max_turns === null ? "" : String(agent.max_turns),
    maxCost: agent.max_cost_usd ?? "",
  };
}

/** The spec that the draft means. Call it only for a draft that `draftProblem` accepts. */
export function toSpec(draft: SubagentDraft): LibrarySpec {
  const turns = draft.maxTurns.trim();
  const cost = draft.maxCost.trim();
  return {
    name: draft.name.trim(),
    description: draft.description.trim(),
    prompt: draft.prompt,
    toolsets: [...draft.toolsets],
    model: draft.model === "" ? null : draft.model,
    max_turns: turns === "" ? null : Number(turns),
    max_cost_usd: cost === "" ? null : cost,
  };
}

export function toCreateBody(
  draft: SubagentDraft,
  scope: SubagentScope,
  workspaceId: string | null,
): LibraryCreate {
  const body: LibraryCreate = { ...toSpec(draft), scope };
  if (scope === "workspace" && workspaceId !== null) body.workspace_id = workspaceId;
  return body;
}

const sameSet = (a: readonly string[], b: readonly string[]) =>
  a.length === b.length && [...a].sort().join("\n") === [...b].sort().join("\n");

/** True when two cost texts have the same number. */
const sameCost = (a: string | null, b: string | null) =>
  a === b || (a !== null && b !== null && Number(a) === Number(b));

/** The fields of `draft` that differ from the saved agent. An empty object: no change. */
export function diffDraft(agent: LibrarySpec, draft: SubagentDraft): LibraryPatch {
  const next = toSpec(draft);
  const patch: LibraryPatch = {};
  if (next.name !== agent.name) patch.name = next.name;
  if (next.description !== agent.description) patch.description = next.description;
  if (next.prompt !== agent.prompt) patch.prompt = next.prompt;
  if (!sameSet(next.toolsets, agent.toolsets)) patch.toolsets = next.toolsets;
  if (next.model !== agent.model) patch.model = next.model;
  if (next.max_turns !== agent.max_turns) patch.max_turns = next.max_turns;
  if (!sameCost(next.max_cost_usd, agent.max_cost_usd)) patch.max_cost_usd = next.max_cost_usd;
  return patch;
}

/** The problem of a name, or `null`. */
export function nameProblem(name: string, limits: LibraryOptions["limits"] = DEFAULT_LIMITS): string | null {
  const text = name.trim();
  if (text === "") return "Give a name.";
  if (text.length > limits.name_max_chars) return `The name has at most ${limits.name_max_chars} characters.`;
  const pattern = safeRegExp(limits.name_pattern);
  if (pattern && !pattern.test(text)) {
    return "Use lowercase letters, digits, and single hyphens. Start with a letter. For example code-reviewer.";
  }
  return null;
}

/** The problem of the max turns text, or `null`. Empty is fine. */
export function turnsProblem(text: string, limits: LibraryOptions["limits"] = DEFAULT_LIMITS): string | null {
  const turns = text.trim();
  if (turns === "") return null;
  const n = Number(turns);
  if (!Number.isInteger(n) || n < 1 || n > limits.max_turns) {
    return `Max turns is a whole number from 1 to ${limits.max_turns}.`;
  }
  return null;
}

/** The problem of the max cost text, or `null`. Empty is fine. */
export function costProblem(text: string, limits: LibraryOptions["limits"] = DEFAULT_LIMITS): string | null {
  const cost = text.trim();
  if (cost === "") return null;
  const max = Number(limits.max_cost_usd);
  if (!DECIMAL.test(cost) || Number(cost) <= 0 || Number(cost) > max) {
    return `Max cost is a number above 0 and up to ${limits.max_cost_usd}, for example 0.50.`;
  }
  return null;
}

/** The first problem that the browser can see before it sends the form, or `null`. */
export function draftProblem(
  draft: SubagentDraft,
  limits: LibraryOptions["limits"] = DEFAULT_LIMITS,
): string | null {
  const name = nameProblem(draft.name, limits);
  if (name !== null) return name;
  const description = draft.description.trim();
  if (description === "") return "Give a description.";
  if (description.length > limits.description_max_chars) {
    return `The description has at most ${limits.description_max_chars} characters.`;
  }
  if (draft.prompt.trim() === "") return "Give a prompt.";
  if (draft.prompt.length > limits.prompt_max_chars) {
    return `The prompt has at most ${limits.prompt_max_chars.toLocaleString()} characters.`;
  }
  return turnsProblem(draft.maxTurns, limits) ?? costProblem(draft.maxCost, limits);
}

/** The problem of the message of a test run, or `null`. */
export function testMessageProblem(message: string): string | null {
  if (message.trim() === "") return "Write a message for the sub-agent.";
  if (message.length > MAX_TEST_MESSAGE_CHARS) {
    return `The message has at most ${MAX_TEST_MESSAGE_CHARS.toLocaleString()} characters.`;
  }
  return null;
}

// -- list ---------------------------------------------------------------------------------------

/** The agents of `scope` (all scopes for `null`) that have all words of `text`. */
export function filterAgents(
  agents: readonly LibraryAgent[],
  scope: SubagentScope | null,
  text: string,
): LibraryAgent[] {
  const words = text.toLowerCase().split(/\s+/).filter(Boolean);
  return agents.filter((a) => {
    if (scope !== null && a.scope !== scope) return false;
    const hay = `${a.name} ${a.description}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

/** The model of an agent for people. */
export function modelLabel(model: string | null): string {
  return model ?? "Same as the run";
}

/** The limits of an agent for a list row, for example `12 turns, 0.50 USD`. `null`: no limit. */
export function limitsText(agent: Pick<LibrarySpec, "max_turns" | "max_cost_usd">): string | null {
  const parts: string[] = [];
  if (agent.max_turns !== null) parts.push(`${agent.max_turns} ${agent.max_turns === 1 ? "turn" : "turns"}`);
  if (agent.max_cost_usd !== null) parts.push(`${agent.max_cost_usd} USD`);
  return parts.length > 0 ? parts.join(", ") : null;
}

/** The names of the agent profiles that use an agent, for a row of the list. */
export function usedByText(usedBy: readonly { name: string }[]): string {
  if (usedBy.length === 0) return "No agent uses it.";
  return `Used by ${usedBy.map((u) => u.name).join(", ")}.`;
}

/** The spec of a version as text, for the history. */
export function specText(spec: LibrarySpec): string {
  const lines = [
    `name: ${spec.name}`,
    `description: ${spec.description}`,
    `tools: ${spec.toolsets.length > 0 ? spec.toolsets.join(", ") : "none"}`,
    `model: ${modelLabel(spec.model)}`,
    `max turns: ${spec.max_turns ?? "default"}`,
    `max cost: ${spec.max_cost_usd === null ? "no limit" : `${spec.max_cost_usd} USD`}`,
    "",
    spec.prompt,
  ];
  return lines.join("\n");
}

// -- agent profile ------------------------------------------------------------------------------

/**
 * The names of the library agents that cannot be added to a profile: the name is already in
 * the profile (an inline sub-agent or another library agent). `ids` are the agents of the
 * profile.
 */
export function takenNames(
  agents: readonly LibraryAgent[],
  ids: readonly string[],
  inlineNames: readonly string[],
): Set<string> {
  const names = new Set(inlineNames.map((n) => n.trim()).filter(Boolean));
  for (const agent of agents) if (ids.includes(agent.id)) names.add(agent.name);
  return names;
}

/** The toolsets of `agent` that the profile does not have. */
export function missingToolsets(agent: Pick<LibrarySpec, "toolsets">, profileToolsets: readonly string[]): string[] {
  return agent.toolsets.filter((t) => !profileToolsets.includes(t));
}
