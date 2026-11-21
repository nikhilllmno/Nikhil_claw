// Helpers for the Harness section of the console: the draft of the runtime, the model, the
// reasoning effort, the busy mode, and the run budget, and its change to a PATCH body. The API
// checks the same limits again: these checks only give an early answer.

import type {
  AgentProfile,
  AgentProfilePatch,
  BudgetCaps,
  BusyMode,
  ProfileBudget,
  ReasoningEffort,
} from "./api";
import { runtimeOption } from "./profile";

export const MAX_TURNS_LIMIT = 10_000;
export const MAX_WALL_TIME_LIMIT_S = 86_400;
export const MAX_TOKENS_LIMIT = 2_147_483_647;

/** The text of each field of the budget. Empty: no limit. The wall time is in minutes. */
export interface BudgetDraft {
  maxTurns: string;
  maxCostUsd: string;
  maxWallMinutes: string;
  maxTokens: string;
}

export interface HarnessDraft {
  runtime: string;
  model: string;
  /** Empty: the default of the model. */
  reasoning: ReasoningEffort | "";
  busyMode: BusyMode;
  budget: BudgetDraft;
}

export const EMPTY_BUDGET: ProfileBudget = {
  max_turns: null,
  max_cost_usd: null,
  max_wall_time_s: null,
  max_tokens: null,
};

/** For example 90 seconds give "1.5". At most two decimals. */
export function secondsToMinutes(seconds: number): string {
  return String(Number((seconds / 60).toFixed(2)));
}

export function toBudgetDraft(budget: ProfileBudget | undefined): BudgetDraft {
  const b = budget ?? EMPTY_BUDGET;
  return {
    maxTurns: b.max_turns === null ? "" : String(b.max_turns),
    maxCostUsd: b.max_cost_usd ?? "",
    maxWallMinutes: b.max_wall_time_s === null ? "" : secondsToMinutes(b.max_wall_time_s),
    maxTokens: b.max_tokens === null ? "" : String(b.max_tokens),
  };
}

export function toHarnessDraft(profile: AgentProfile): HarnessDraft {
  return {
    runtime: profile.runtime,
    model: profile.model,
    reasoning: profile.reasoning_effort ?? "",
    busyMode: profile.busy_mode ?? "steer",
    budget: toBudgetDraft(profile.budget),
  };
}

/** The draft after the user chooses `runtime`: a model of another runtime cannot stay. */
export function withHarnessRuntime(draft: HarnessDraft, profile: AgentProfile, runtime: string): HarnessDraft {
  const { models } = runtimeOption(profile.options, profile.runtime, runtime);
  let model = draft.model;
  if (runtime === profile.runtime) model = profile.model;
  else if (!models.includes(model)) model = models[0] ?? "";
  return { ...draft, runtime, model };
}

const WHOLE = /^\d+$/;
const DECIMAL = /^\d+(\.\d+)?$/;

/** The budget that a draft means. A field that the user did not change keeps its saved value. */
export function budgetFromDraft(draft: BudgetDraft, saved: ProfileBudget | undefined): ProfileBudget {
  const base = saved ?? EMPTY_BUDGET;
  const before = toBudgetDraft(saved);
  const turns = draft.maxTurns.trim();
  const cost = draft.maxCostUsd.trim();
  const minutes = draft.maxWallMinutes.trim();
  const tokens = draft.maxTokens.trim();
  return {
    max_turns: turns === before.maxTurns ? base.max_turns : turns === "" ? null : Number(turns),
    max_cost_usd: cost === before.maxCostUsd ? base.max_cost_usd : cost === "" ? null : cost,
    max_wall_time_s:
      minutes === before.maxWallMinutes
        ? base.max_wall_time_s
        : minutes === ""
          ? null
          : Math.round(Number(minutes) * 60),
    max_tokens: tokens === before.maxTokens ? base.max_tokens : tokens === "" ? null : Number(tokens),
  };
}

export function budgetChanged(draft: BudgetDraft, saved: ProfileBudget | undefined): boolean {
  const before = toBudgetDraft(saved);
  return (
    draft.maxTurns.trim() !== before.maxTurns ||
    draft.maxCostUsd.trim() !== before.maxCostUsd ||
    draft.maxWallMinutes.trim() !== before.maxWallMinutes ||
    draft.maxTokens.trim() !== before.maxTokens
  );
}

/** The first problem of the budget draft, or `null`. `caps` is the run budget of the plan. */
export function validateBudget(draft: BudgetDraft, caps: BudgetCaps | undefined): string | null {
  const turns = draft.maxTurns.trim();
  if (turns === "" && caps?.max_turns != null) {
    return `This plan needs a limit for max turns: at most ${caps.max_turns.toLocaleString("en-US")}.`;
  }
  if (turns !== "") {
    const n = Number(turns);
    if (!WHOLE.test(turns) || n < 1 || n > MAX_TURNS_LIMIT) {
      return `Max turns is a whole number from 1 to ${MAX_TURNS_LIMIT.toLocaleString("en-US")}.`;
    }
    if (caps?.max_turns != null && n > caps.max_turns) {
      return `Max turns is at most ${caps.max_turns.toLocaleString("en-US")} on this plan.`;
    }
  }
  const cost = draft.maxCostUsd.trim();
  if (cost === "" && caps?.max_cost_usd != null) {
    return `This plan needs a limit for max cost: at most ${caps.max_cost_usd} US dollars.`;
  }
  if (cost !== "") {
    const n = Number(cost);
    if (!DECIMAL.test(cost) || !(n > 0)) return "Max cost is a number of US dollars above 0.";
    if (caps?.max_cost_usd != null && n > Number(caps.max_cost_usd)) {
      return `Max cost is at most ${caps.max_cost_usd} US dollars on this plan.`;
    }
  }
  const minutes = draft.maxWallMinutes.trim();
  if (minutes === "" && caps?.max_wall_time_s != null) {
    return `This plan needs a limit for max wall time: at most ${secondsToMinutes(caps.max_wall_time_s)} minutes.`;
  }
  if (minutes !== "") {
    const n = Number(minutes);
    if (!DECIMAL.test(minutes) || !(n > 0)) return "Max wall time is a number of minutes above 0.";
    const seconds = Math.round(n * 60);
    if (seconds < 1) return "Max wall time is at least 1 second.";
    if (seconds > MAX_WALL_TIME_LIMIT_S) {
      return `Max wall time is at most ${MAX_WALL_TIME_LIMIT_S / 60} minutes.`;
    }
    if (caps?.max_wall_time_s != null && seconds > caps.max_wall_time_s) {
      return `Max wall time is at most ${secondsToMinutes(caps.max_wall_time_s)} minutes on this plan.`;
    }
  }
  const tokens = draft.maxTokens.trim();
  if (tokens !== "") {
    const n = Number(tokens);
    if (!WHOLE.test(tokens) || n < 1 || n > MAX_TOKENS_LIMIT) {
      return "Max tokens is a whole number above 0.";
    }
  }
  return null;
}

/** The first problem of the draft, or `null`. */
export function validateHarness(draft: HarnessDraft, caps: BudgetCaps | undefined): string | null {
  if (draft.model === "") return "Choose a model.";
  return validateBudget(draft.budget, caps);
}

/** Only the fields that differ from the saved profile. An empty object: no change. */
export function harnessPatch(profile: AgentProfile, draft: HarnessDraft): AgentProfilePatch {
  const patch: AgentProfilePatch = {};
  if (draft.runtime !== profile.runtime) {
    // The API needs a model of the new runtime, so the patch always has the model.
    patch.runtime = draft.runtime;
    patch.model = draft.model;
  } else if (draft.model !== profile.model) {
    patch.model = draft.model;
  }
  if (profile.options.reasoning_efforts !== undefined) {
    const effort = draft.reasoning === "" ? null : draft.reasoning;
    if (effort !== (profile.reasoning_effort ?? null)) patch.reasoning_effort = effort;
  }
  if (profile.options.busy_modes !== undefined && draft.busyMode !== (profile.busy_mode ?? "steer")) {
    patch.busy_mode = draft.busyMode;
  }
  if (profile.budget !== undefined && budgetChanged(draft.budget, profile.budget)) {
    patch.budget = budgetFromDraft(draft.budget, profile.budget);
  }
  return patch;
}

// -- labels -------------------------------------------------------------------------------------

export function reasoningLabel(effort: ReasoningEffort | ""): string {
  switch (effort) {
    case "low":
      return "Low";
    case "medium":
      return "Medium";
    case "high":
      return "High";
    default:
      return "Model default";
  }
}

export function busyModeLabel(mode: BusyMode): string {
  switch (mode) {
    case "steer":
      return "Steer";
    case "parallel":
      return "Parallel";
    case "queue":
      return "Queue";
  }
}

export function busyModeHelp(mode: BusyMode): string {
  switch (mode) {
    case "steer":
      return "A new message joins the open run. This is the default.";
    case "parallel":
      return "A new message starts a second run in the same sandbox.";
    case "queue":
      return "A new message waits until the open run ends.";
  }
}

/** The hint under a budget field: what the plan allows. */
export function capHint(cap: number | string | null | undefined, unit: string): string {
  if (cap === null || cap === undefined) return "Empty means no limit. The plan has no cap.";
  return `The plan needs a limit of at most ${cap} ${unit}.`;
}
