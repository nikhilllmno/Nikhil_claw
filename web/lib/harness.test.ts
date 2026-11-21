import { describe, expect, it } from "vitest";
import { withRuntime } from "./profile";
import type { AgentProfile } from "./api";
import {
  budgetChanged,
  budgetFromDraft,
  busyModeHelp,
  busyModeLabel,
  capHint,
  harnessPatch,
  reasoningLabel,
  EMPTY_BUDGET,
  secondsToMinutes,
  toBudgetDraft,
  toHarnessDraft,
  validateBudget,
  validateHarness,
} from "./harness";

function profile(overrides: Partial<AgentProfile> = {}): AgentProfile {
  return {
    id: "p1",
    org_id: "o1",
    workspace_id: null,
    name: "Assistant",
    soul: "",
    runtime: "claude_agent_sdk",
    model: "claude-sonnet",
    toolsets: [],
    group_toolsets: [],
    subagents: [],
    updated_at: "2026-11-20T00:00:00+00:00",
    editable: true,
    options: {
      toolsets: [],
      models: ["claude-sonnet", "claude-opus"],
      subagents: true,
      runtimes: [
        { name: "claude_agent_sdk", models: ["claude-sonnet", "claude-opus"], subagents: true },
        { name: "openai_agents_sdk", models: ["gpt-5"], subagents: false },
      ],
      limits: {
        name_max_chars: 80,
        soul_max_chars: 20000,
        max_subagents: 10,
        subagent_name_max_chars: 40,
        subagent_name_pattern: "^[a-z]",
        subagent_description_max_chars: 200,
        subagent_prompt_max_chars: 8000,
        subagent_max_turns: 50,
      },
      reasoning_efforts: ["low", "medium", "high"],
      budget_caps: { max_turns: 100, max_cost_usd: "5.00", max_wall_time_s: 3600 },
    },
    budget: { max_turns: 40, max_cost_usd: "1.00", max_wall_time_s: 1800, max_tokens: null },
    reasoning_effort: null,
    ...overrides,
  };
}

describe("budget draft", () => {
  it("shows the wall time in minutes", () => {
    expect(secondsToMinutes(1800)).toBe("30");
    expect(secondsToMinutes(90)).toBe("1.5");
    expect(secondsToMinutes(100)).toBe("1.67");
    expect(toBudgetDraft(profile().budget)).toEqual({
      maxTurns: "40",
      maxCostUsd: "1.00",
      maxWallMinutes: "30",
      maxTokens: "",
    });
  });

  it("shows empty fields when there is no budget", () => {
    expect(toBudgetDraft(EMPTY_BUDGET)).toEqual({
      maxTurns: "",
      maxCostUsd: "",
      maxWallMinutes: "",
      maxTokens: "",
    });
  });

  it("sends the wall time in seconds", () => {
    const saved = profile().budget;
    const draft = { ...toBudgetDraft(saved), maxWallMinutes: "2.5" };
    expect(budgetChanged(draft, saved)).toBe(true);
    expect(budgetFromDraft(draft, saved)).toEqual({
      max_turns: 40,
      max_cost_usd: "1.00",
      max_wall_time_s: 150,
      max_tokens: null,
    });
  });

  it("keeps a saved value that the user did not change", () => {
    const saved = { max_turns: null, max_cost_usd: null, max_wall_time_s: 100, max_tokens: null };
    const draft = { ...toBudgetDraft(saved), maxTurns: "5" };
    expect(budgetFromDraft(draft, saved).max_wall_time_s).toBe(100);
    expect(budgetFromDraft(draft, saved).max_turns).toBe(5);
  });

  it("sends null for an empty field", () => {
    const saved = profile().budget;
    const draft = { ...toBudgetDraft(saved), maxCostUsd: " ", maxTurns: "" };
    expect(budgetFromDraft(draft, saved)).toEqual({
      max_turns: null,
      max_cost_usd: null,
      max_wall_time_s: 1800,
      max_tokens: null,
    });
  });

  it("does not see a change in the saved draft", () => {
    expect(budgetChanged(toBudgetDraft(profile().budget), profile().budget)).toBe(false);
  });
});

describe("validateBudget", () => {
  const caps = { max_turns: 100, max_cost_usd: "5.00", max_wall_time_s: 3600 };
  const empty = toBudgetDraft(EMPTY_BUDGET);
  // A draft that sets every capped value, so one test checks one field.
  const full = { ...empty, maxTurns: "100", maxCostUsd: "5", maxWallMinutes: "60" };

  it("accepts empty fields when the plan has no cap", () => {
    expect(validateBudget(empty, undefined)).toBeNull();
    expect(validateBudget(empty, { max_turns: null, max_cost_usd: null, max_wall_time_s: null })).toBeNull();
  });

  it("needs a value for each capped field", () => {
    expect(validateBudget(empty, caps)).toContain("needs a limit for max turns");
    expect(validateBudget({ ...full, maxCostUsd: "" }, caps)).toContain("needs a limit for max cost");
    expect(validateBudget({ ...full, maxWallMinutes: "" }, caps)).toContain("needs a limit for max wall time");
    expect(validateBudget(full, caps)).toBeNull();
  });

  it("checks the turns", () => {
    expect(validateBudget({ ...full, maxTurns: "0" }, caps)).toContain("Max turns");
    expect(validateBudget({ ...full, maxTurns: "1.5" }, caps)).toContain("Max turns");
    expect(validateBudget({ ...empty, maxTurns: "20000" }, undefined)).toContain("10,000");
    expect(validateBudget({ ...full, maxTurns: "101" }, caps)).toContain("on this plan");
    expect(validateBudget({ ...full, maxTurns: "100" }, caps)).toBeNull();
  });

  it("checks the cost against the cap", () => {
    expect(validateBudget({ ...full, maxCostUsd: "abc" }, caps)).toContain("Max cost");
    expect(validateBudget({ ...full, maxCostUsd: "0" }, caps)).toContain("Max cost");
    expect(validateBudget({ ...full, maxCostUsd: "5.01" }, caps)).toContain("5.00");
    expect(validateBudget({ ...full, maxCostUsd: "5" }, caps)).toBeNull();
    expect(validateBudget({ ...full, maxCostUsd: "500" }, { ...caps, max_cost_usd: null })).toBeNull();
  });

  it("checks the wall time in minutes against the cap", () => {
    expect(validateBudget({ ...full, maxWallMinutes: "-1" }, caps)).toContain("Max wall time");
    expect(validateBudget({ ...full, maxWallMinutes: "61" }, caps)).toContain("60 minutes on this plan");
    expect(validateBudget({ ...full, maxWallMinutes: "60" }, caps)).toBeNull();
    expect(validateBudget({ ...empty, maxWallMinutes: "2000" }, undefined)).toContain("1440");
  });

  it("checks the tokens", () => {
    expect(validateBudget({ ...full, maxTokens: "0" }, caps)).toContain("Max tokens");
    expect(validateBudget({ ...full, maxTokens: "1e3" }, caps)).toContain("Max tokens");
    expect(validateBudget({ ...full, maxTokens: "50000" }, caps)).toBeNull();
  });
});

describe("harnessPatch", () => {
  it("sends nothing when nothing changed", () => {
    const p = profile();
    expect(harnessPatch(p, toHarnessDraft(p))).toEqual({});
  });

  it("sends the model of a model change", () => {
    const p = profile();
    expect(harnessPatch(p, { ...toHarnessDraft(p), model: "claude-opus" })).toEqual({ model: "claude-opus" });
  });

  it("sends the model with a new runtime", () => {
    const p = profile();
    const draft = withRuntime(toHarnessDraft(p), p, "openai_agents_sdk");
    expect(draft.model).toBe("gpt-5");
    expect(harnessPatch(p, draft)).toEqual({ runtime: "openai_agents_sdk", model: "gpt-5" });
    expect(withRuntime(draft, p, "claude_agent_sdk").model).toBe("claude-sonnet");
  });

  it("sends the reasoning effort, and null for the model default", () => {
    const p = profile();
    expect(harnessPatch(p, { ...toHarnessDraft(p), reasoning: "high" })).toEqual({ reasoning_effort: "high" });
    const set = profile({ reasoning_effort: "low" });
    expect(harnessPatch(set, { ...toHarnessDraft(set), reasoning: "" })).toEqual({ reasoning_effort: null });
  });

  it("sends the busy mode only when the API has it", () => {
    const p = profile();
    expect(harnessPatch(p, { ...toHarnessDraft(p), busyMode: "queue" })).toEqual({});
    p.options.busy_modes = ["steer", "parallel", "queue"];
    expect(harnessPatch(p, { ...toHarnessDraft(p), busyMode: "queue" })).toEqual({ busy_mode: "queue" });
    p.busy_mode = "queue";
    expect(harnessPatch(p, toHarnessDraft(p))).toEqual({});
  });

  it("sends the whole budget when one field changed", () => {
    const p = profile();
    const draft = toHarnessDraft(p);
    draft.budget.maxTokens = "9000";
    expect(harnessPatch(p, draft)).toEqual({
      budget: { max_turns: 40, max_cost_usd: "1.00", max_wall_time_s: 1800, max_tokens: 9000 },
    });
  });
});

describe("validateHarness", () => {
  it("needs a model", () => {
    const p = profile();
    expect(validateHarness({ ...toHarnessDraft(p), model: "" }, undefined)).toBe("Choose a model.");
    expect(validateHarness(toHarnessDraft(p), p.options.budget_caps)).toBeNull();
  });
});

describe("labels", () => {
  it("names the reasoning efforts", () => {
    expect(reasoningLabel("")).toBe("Model default");
    expect(reasoningLabel("high")).toBe("High");
  });

  it("names and explains the busy modes", () => {
    expect(busyModeLabel("steer")).toBe("Steer");
    expect(busyModeHelp("steer")).toContain("joins the open run");
    expect(busyModeHelp("parallel")).toContain("second run");
    expect(busyModeHelp("queue")).toContain("waits");
  });

  it("says what the plan allows", () => {
    expect(capHint(null, "turns")).toContain("no cap");
    expect(capHint(100, "turns")).toBe("The plan needs a limit of at most 100 turns.");
  });
});
