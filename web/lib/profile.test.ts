import { describe, expect, it } from "vitest";
import type { AgentProfile } from "./api";
import { diffDraft, newSubagent, toDraft, validateDraft } from "./profile";

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
  updated_at: "2026-11-20T00:00:00Z",
  editable: true,
  options: {
    toolsets: [],
    models: ["claude-sonnet-5-5"],
    subagents: true,
    limits: { soul_max_chars: 20000, subagent_name_pattern: "^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", subagent_max_turns: 50 },
  },
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
