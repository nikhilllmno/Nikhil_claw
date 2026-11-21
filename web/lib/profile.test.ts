import { describe, expect, it } from "vitest";
import type { AgentProfile } from "./api";
import { diffDraft, memoryProviderLabel, newSubagent, runtimeOption, toDraft, validateDraft, withRuntime } from "./profile";

const profile: AgentProfile = {
  id: "p",
  org_id: "o",
  workspace_id: null,
  name: "sama",
  soul: "Be brief.",
  runtime: "claude_agent_sdk",
  model: "claude-sonnet-5-5",
  toolsets: ["core", "web"],
  group_toolsets: [],
  subagents: [
    {
      name: "researcher",
      description: "Reads the web.",
      prompt: "Find facts.",
      toolsets: ["web"],
      model: null,
      max_turns: 8,
    },
  ],
  library_subagents: [],
  delegation_model: null,
  memory_provider: "builtin",
  updated_at: "2026-11-20T00:00:00Z",
  editable: true,
  options: {
    toolsets: [],
    models: ["claude-sonnet-5-5"],
    subagents: true,
    runtimes: [
      { name: "claude_agent_sdk", models: ["claude-opus-5-5", "claude-sonnet-5-5"], subagents: true },
      { name: "openai_agents_sdk", models: ["gpt-6.2-astra", "gpt-6.2-sol"], subagents: false },
    ],
    memory_providers: ["builtin", "mem0"],
    limits: {
      name_max_chars: 80,
      soul_max_chars: 20000,
      max_subagents: 10,
      subagent_name_max_chars: 40,
      subagent_name_pattern: "^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
      subagent_description_max_chars: 400,
      subagent_prompt_max_chars: 20000,
      subagent_max_turns: 50,
    },
    reasoning_efforts: ["low", "medium", "high"],
    budget_caps: { max_turns: null, max_cost_usd: null, max_wall_time_s: null },
  },
  budget: { max_turns: null, max_cost_usd: null, max_wall_time_s: null, max_tokens: null },
  reasoning_effort: null,
};

describe("diffDraft", () => {
  it("is empty for an untouched draft", () => {
    expect(diffDraft(profile, toDraft(profile))).toEqual({});
  });

  it("has only the changed fields", () => {
    const draft = toDraft(profile);
    draft.soul = "Be kind.";
    draft.toolsets = ["web", "core"]; // same set, other order: no change
    expect(diffDraft(profile, draft)).toEqual({ soul: "Be kind." });
    draft.toolsets = ["core"];
    draft.name = "  new name ";
    expect(diffDraft(profile, draft)).toEqual({ soul: "Be kind.", toolsets: ["core"], name: "new name" });
  });

  it("sends all sub-agents when one changes, with a number for max turns", () => {
    const draft = toDraft(profile);
    draft.subagents[0]!.maxTurns = "12";
    draft.subagents.push({ ...newSubagent(), name: "writer", description: "d", prompt: "p", model: "m" });
    const patch = diffDraft(profile, draft);
    expect(patch.subagents).toHaveLength(2);
    expect(patch.subagents?.[0]?.max_turns).toBe(12);
    expect(patch.subagents?.[1]).toMatchObject({ name: "writer", model: "m", max_turns: null });
  });
});

describe("library sub-agents", () => {
  it("sends the ids when the list changes, and checks the count with the inline agents", () => {
    const draft = toDraft(profile);
    draft.librarySubagents = ["a1"];
    expect(diffDraft(profile, draft)).toEqual({ library_subagents: ["a1"] });
    const saved = { ...profile, library_subagents: ["a1", "a2"] };
    const same = toDraft(saved);
    expect(diffDraft(saved, same)).toEqual({});
    same.librarySubagents = ["a2", "a1"]; // the order is part of the profile
    expect(diffDraft(saved, same)).toEqual({ library_subagents: ["a2", "a1"] });
    const full = toDraft(profile);
    full.librarySubagents = Array.from({ length: 10 }, (_, i) => `id${i}`); // 1 inline + 10
    expect(validateDraft(full, profile.options.limits)).toMatch(/at most 10/);
    full.librarySubagents = full.librarySubagents.slice(0, 9);
    expect(validateDraft(full, profile.options.limits)).toBeNull();
  });
});

describe("delegation model and memory provider", () => {
  it("starts from the saved values", () => {
    expect(toDraft(profile)).toMatchObject({ delegationModel: "", memoryProvider: "builtin" });
    const saved = { ...profile, delegation_model: "claude-opus-5-5", memory_provider: "mem0" as const };
    expect(toDraft(saved)).toMatchObject({ delegationModel: "claude-opus-5-5", memoryProvider: "mem0" });
    expect(diffDraft(saved, toDraft(saved))).toEqual({});
  });

  it("sends only the changed field", () => {
    const draft = toDraft(profile);
    draft.delegationModel = "claude-opus-5-5";
    expect(diffDraft(profile, draft)).toEqual({ delegation_model: "claude-opus-5-5" });
    draft.memoryProvider = "mem0";
    expect(diffDraft(profile, draft)).toEqual({ delegation_model: "claude-opus-5-5", memory_provider: "mem0" });
  });

  it("sends an empty string to go back to the model of the agent", () => {
    const saved = { ...profile, delegation_model: "claude-opus-5-5" };
    const draft = { ...toDraft(saved), delegationModel: "" };
    expect(diffDraft(saved, draft)).toEqual({ delegation_model: "" });
  });

  it("drops a delegation model that the new runtime does not have", () => {
    const saved = { ...profile, delegation_model: "claude-opus-5-5" };
    const draft = withRuntime(toDraft(saved), saved, "openai_agents_sdk");
    expect(draft.delegationModel).toBe("");
    expect(diffDraft(saved, draft)).toMatchObject({ delegation_model: "" });
    expect(withRuntime(draft, saved, "claude_agent_sdk").delegationModel).toBe("claude-opus-5-5");
  });

  it("names the providers for people", () => {
    expect(memoryProviderLabel("builtin")).toBe("Built-in only");
    expect(memoryProviderLabel("mem0")).toBe("Built-in and Mem0");
    expect(memoryProviderLabel("other")).toBe("other");
  });
});

describe("validateDraft", () => {
  it("accepts the saved profile", () => {
    expect(validateDraft(toDraft(profile), profile.options.limits)).toBeNull();
  });

  it("finds the first problem", () => {
    const draft = toDraft(profile);
    draft.name = " ";
    expect(validateDraft(draft, profile.options.limits)).toMatch(/needs a name/);
    draft.name = "sama";
    draft.subagents[0]!.name = "Bad Name";
    expect(validateDraft(draft, profile.options.limits)).toMatch(/lowercase/);
    draft.subagents[0]!.name = "ok";
    draft.subagents[0]!.maxTurns = "99";
    expect(validateDraft(draft, profile.options.limits)).toMatch(/1 to 50/);
  });
});

describe("the runtime", () => {
  it("takes a model of the new runtime and sends both fields", () => {
    const draft = withRuntime(toDraft(profile), profile, "openai_agents_sdk");
    expect(draft.model).toBe("gpt-6.2-astra");
    draft.subagents = [];
    expect(diffDraft(profile, draft)).toMatchObject({
      runtime: "openai_agents_sdk",
      model: "gpt-6.2-astra",
      subagents: [],
    });
  });

  it("keeps a model that the new runtime has, and goes back to the saved model", () => {
    const draft = withRuntime({ ...toDraft(profile), model: "gpt-6.2-sol" }, profile, "openai_agents_sdk");
    expect(draft.model).toBe("gpt-6.2-sol");
    const back = withRuntime(draft, profile, "claude_agent_sdk");
    expect(back.model).toBe(profile.model);
    expect(diffDraft(profile, back)).toEqual({});
  });

  it("refuses sub-agents on a runtime without them", () => {
    const draft = withRuntime(toDraft(profile), profile, "openai_agents_sdk");
    const option = runtimeOption(profile.options, profile.runtime, draft.runtime);
    expect(option.subagents).toBe(false);
    expect(validateDraft(draft, profile.options.limits, option)).toMatch(/has no sub-agents/);
    draft.subagents = [];
    expect(validateDraft(draft, profile.options.limits, option)).toBeNull();
  });

  it("uses the profile options when the list does not have the saved runtime", () => {
    const old = { ...profile.options, runtimes: [] };
    expect(runtimeOption(old, "claude_agent_sdk", "claude_agent_sdk")).toEqual({
      name: "claude_agent_sdk",
      models: ["claude-sonnet-5-5"],
      subagents: true,
    });
    expect(runtimeOption(old, "claude_agent_sdk", "other").models).toEqual([]);
  });
});
