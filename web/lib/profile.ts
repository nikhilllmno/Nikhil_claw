// The draft of the agent settings form and its change to a PATCH body.

import type {
  AgentProfile,
  AgentProfileLimits,
  AgentProfileOptions,
  AgentProfilePatch,
  RuntimeOption,
  Subagent,
} from "./api";

export interface DraftSubagent {
  /** A key for React. It is not sent. */
  uid: number;
  name: string;
  description: string;
  prompt: string;
  toolsets: string[];
  /** Empty: inherit the model of the agent. */
  model: string;
  /** Text of the field. Empty: the default. */
  maxTurns: string;
}

export interface Draft {
  name: string;
  soul: string;
  runtime: string;
  model: string;
  toolsets: string[];
  subagents: DraftSubagent[];
}

let nextUid = 1;

export function newSubagent(): DraftSubagent {
  return {
    uid: nextUid++,
    name: "",
    description: "",
    prompt: "",
    toolsets: [],
    model: "",
    maxTurns: "",
  };
}

export function toDraft(profile: AgentProfile): Draft {
  return {
    name: profile.name,
    soul: profile.soul,
    runtime: profile.runtime,
    model: profile.model,
    toolsets: [...profile.toolsets],
    subagents: profile.subagents.map((s) => ({
      uid: nextUid++,
      name: s.name,
      description: s.description,
      prompt: s.prompt,
      toolsets: [...s.toolsets],
      model: s.model ?? "",
      maxTurns: s.max_turns === null ? "" : String(s.max_turns),
    })),
  };
}

export function toSubagent(draft: DraftSubagent): Subagent {
  const turns = draft.maxTurns.trim();
  return {
    name: draft.name.trim(),
    description: draft.description.trim(),
    prompt: draft.prompt,
    toolsets: draft.toolsets,
    model: draft.model === "" ? null : draft.model,
    max_turns: turns === "" ? null : Number(turns),
  };
}

export const sameList = (a: readonly string[], b: readonly string[]) =>
  a.length === b.length && [...a].sort().join("\n") === [...b].sort().join("\n");

/** The fields of `draft` that differ from the saved profile. An empty object: no change. */
export function diffDraft(profile: AgentProfile, draft: Draft): AgentProfilePatch {
  const patch: AgentProfilePatch = {};
  if (draft.name.trim() !== profile.name) patch.name = draft.name.trim();
  if (draft.soul !== profile.soul) patch.soul = draft.soul;
  if (draft.runtime !== profile.runtime) {
    // The API needs a model of the new runtime, so the patch always has the model.
    patch.runtime = draft.runtime;
    patch.model = draft.model;
  } else if (draft.model !== profile.model) {
    patch.model = draft.model;
  }
  if (!sameList(draft.toolsets, profile.toolsets)) patch.toolsets = draft.toolsets;
  const subagents = draft.subagents.map(toSubagent);
  if (JSON.stringify(subagents) !== JSON.stringify(profile.subagents)) patch.subagents = subagents;
  return patch;
}

const RUNTIME_LABELS: Record<string, string> = {
  claude_agent_sdk: "Claude Agent SDK",
  openai_agents_sdk: "OpenAI Agents SDK",
};

/** The name of a runtime for people. */
export function runtimeLabel(runtime: string): string {
  return RUNTIME_LABELS[runtime] ?? runtime;
}

/** The option of `runtime`. For the saved runtime, the options of the profile are the fallback. */
export function runtimeOption(options: AgentProfileOptions, saved: string, runtime: string): RuntimeOption {
  const found = options.runtimes.find((r) => r.name === runtime);
  if (found) return found;
  if (runtime === saved) return { name: runtime, models: options.models, subagents: options.subagents };
  return { name: runtime, models: [], subagents: false };
}

/**
 * The draft after the user chooses `runtime`. A model of another runtime cannot stay, so the
 * draft takes the saved model (back on the saved runtime) or the first model of the runtime.
 */
export function withRuntime<D extends { runtime: string; model: string }>(
  draft: D,
  profile: AgentProfile,
  runtime: string,
): D {
  const { models } = runtimeOption(profile.options, profile.runtime, runtime);
  let model = draft.model;
  if (runtime === profile.runtime) model = profile.model;
  else if (!models.includes(model)) model = models[0] ?? "";
  return { ...draft, runtime, model };
}

/** `list` with `value` in it, so that a select shows a value that the options do not have. */
export function withCurrent(list: string[], value: string): string[] {
  return value === "" || list.includes(value) ? list : [value, ...list];
}

/** The first problem that the browser can see before it sends the form, or `null`. */
export function validateDraft(
  draft: Draft,
  limits: AgentProfileLimits,
  runtime?: RuntimeOption,
): string | null {
  if (draft.name.trim() === "") return "The agent needs a name.";
  if (draft.model === "") return "Choose a model.";
  if (runtime && !runtime.subagents && draft.subagents.length > 0) {
    return `The ${runtimeLabel(runtime.name)} runtime has no sub-agents. Remove the sub-agents first.`;
  }
  if (draft.name.trim().length > limits.name_max_chars) {
    return `The name has at most ${limits.name_max_chars} characters.`;
  }
  if (draft.soul.length > limits.soul_max_chars) {
    return `The soul has at most ${limits.soul_max_chars.toLocaleString()} characters.`;
  }
  const pattern = safeRegExp(limits.subagent_name_pattern);
  const names = new Set<string>();
  for (const [index, sub] of draft.subagents.entries()) {
    const label = sub.name.trim() || `Sub-agent ${index + 1}`;
    if (sub.name.trim() === "") return `${label} needs a name.`;
    if (pattern && !pattern.test(sub.name.trim())) {
      return `${label}: use lowercase letters, digits, and single hyphens, and start with a letter.`;
    }
    if (names.has(sub.name.trim())) return `Two sub-agents have the name ${sub.name.trim()}.`;
    names.add(sub.name.trim());
    if (sub.description.trim() === "") return `${label} needs a description.`;
    if (sub.prompt.trim() === "") return `${label} needs a prompt.`;
    const turns = sub.maxTurns.trim();
    if (turns !== "") {
      const n = Number(turns);
      if (!Number.isInteger(n) || n < 1 || n > limits.subagent_max_turns) {
        return `${label}: max turns is a whole number from 1 to ${limits.subagent_max_turns}.`;
      }
    }
  }
  return null;
}

function safeRegExp(source: string): RegExp | null {
  try {
    return new RegExp(source);
  } catch {
    return null;
  }
}
