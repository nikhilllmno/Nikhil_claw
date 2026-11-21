import { describe, expect, it } from "vitest";
import {
  type LibraryAgent,
  type SubagentDraft,
  costProblem,
  diffDraft,
  draftProblem,
  emptyDraft,
  filterAgents,
  limitsText,
  missingToolsets,
  nameProblem,
  SCOPE_LABELS,
  specText,
  takenNames,
  testMessageProblem,
  toCreateBody,
  toDraft,
  toSpec,
  turnsProblem,
  usedByText,
  versionSource,
} from "./subagents";

function agent(over: Partial<LibraryAgent> = {}): LibraryAgent {
  return {
    id: "a1",
    name: "reviewer",
    description: "Reviews code.",
    prompt: "Find bugs.",
    toolsets: ["web", "core"],
    model: null,
    max_turns: 8,
    max_cost_usd: "0.50",
    scope: "workspace",
    workspace_id: "w1",
    user_id: null,
    version: 2,
    updated_at: "2026-11-20T00:00:00Z",
    can_edit: true,
    used_by: [],
    ...over,
  };
}

function valid(): SubagentDraft {
  return { ...emptyDraft(), name: "writer", description: "Writes.", prompt: "Write well." };
}

describe("scope labels", () => {
  it("shows Personal, Workspace, and Org", () => {
    expect(SCOPE_LABELS).toEqual({ user: "Personal", workspace: "Workspace", org: "Org" });
  });
});

describe("checks", () => {
  it("checks the name", () => {
    expect(nameProblem("code-reviewer")).toBeNull();
    expect(nameProblem(" ")).toBe("Give a name.");
    expect(nameProblem("1abc")).toMatch(/lowercase/);
    expect(nameProblem("a--b")).toMatch(/lowercase/);
    expect(nameProblem("Abc")).toMatch(/lowercase/);
    expect(nameProblem("a".repeat(41))).toMatch(/40/);
  });

  it("checks max turns from 1 to 50, or empty", () => {
    expect(turnsProblem("")).toBeNull();
    expect(turnsProblem("1")).toBeNull();
    expect(turnsProblem("50")).toBeNull();
    expect(turnsProblem("0")).toMatch(/1 to 50/);
    expect(turnsProblem("51")).toMatch(/1 to 50/);
    expect(turnsProblem("2.5")).toMatch(/1 to 50/);
  });

  it("checks max cost above 0 and up to 10, or empty", () => {
    expect(costProblem("")).toBeNull();
    expect(costProblem("0.50")).toBeNull();
    expect(costProblem("10")).toBeNull();
    expect(costProblem("0")).toMatch(/above 0/);
    expect(costProblem("10.01")).toMatch(/up to 10/);
    expect(costProblem("-1")).not.toBeNull();
    expect(costProblem("1e1")).not.toBeNull();
    expect(costProblem("abc")).not.toBeNull();
  });

  it("finds the first problem of a draft", () => {
    expect(draftProblem(valid())).toBeNull();
    expect(draftProblem({ ...valid(), name: "" })).toBe("Give a name.");
    expect(draftProblem({ ...valid(), description: " " })).toBe("Give a description.");
    expect(draftProblem({ ...valid(), description: "d".repeat(301) })).toMatch(/300/);
    expect(draftProblem({ ...valid(), prompt: "" })).toBe("Give a prompt.");
    expect(draftProblem({ ...valid(), prompt: "p".repeat(8001) })).toMatch(/8,?000/);
    expect(draftProblem({ ...valid(), maxTurns: "99" })).toMatch(/1 to 50/);
    expect(draftProblem({ ...valid(), maxCost: "11" })).toMatch(/up to 10/);
  });

  it("uses the limits of the options", () => {
    const limits = { ...{ name_max_chars: 5, name_pattern: "^[a-z]+$", description_max_chars: 300, prompt_max_chars: 8000, max_turns: 5, max_cost_usd: "1" } };
    expect(nameProblem("abcdef", limits)).toMatch(/5/);
    expect(nameProblem("a-b", limits)).toMatch(/lowercase/);
    expect(turnsProblem("6", limits)).toMatch(/1 to 5/);
    expect(costProblem("2", limits)).toMatch(/up to 1/);
  });

  it("checks the message of a test run", () => {
    expect(testMessageProblem("")).not.toBeNull();
    expect(testMessageProblem("hi")).toBeNull();
    expect(testMessageProblem("x".repeat(4001))).toMatch(/4,?000/);
  });
});

describe("draft", () => {
  it("is empty for an untouched draft", () => {
    expect(diffDraft(agent(), toDraft(agent()))).toEqual({});
  });

  it("turns a draft into a spec with numbers and nulls", () => {
    const draft = { ...valid(), name: " writer ", maxTurns: " 12 ", maxCost: "0.5", model: "m" };
    expect(toSpec(draft)).toMatchObject({ name: "writer", max_turns: 12, max_cost_usd: "0.5", model: "m" });
    expect(toSpec(valid())).toMatchObject({ model: null, max_turns: null, max_cost_usd: null, toolsets: [] });
  });

  it("has only the changed fields", () => {
    const draft = toDraft(agent());
    draft.description = "Reviews code well.";
    draft.toolsets = ["core", "web"]; // same set, other order: no change
    draft.maxCost = "0.5"; // same number: no change
    expect(diffDraft(agent(), draft)).toEqual({ description: "Reviews code well." });
    draft.toolsets = ["core"];
    draft.name = " new-name ";
    expect(diffDraft(agent(), draft)).toEqual({
      description: "Reviews code well.",
      toolsets: ["core"],
      name: "new-name",
    });
  });

  it("sends null to clear the model, the turns, and the cost", () => {
    const set = agent({ model: "claude-sonnet-5-5" });
    const draft = toDraft(set);
    draft.model = "";
    draft.maxTurns = "";
    draft.maxCost = "";
    expect(diffDraft(set, draft)).toEqual({ model: null, max_turns: null, max_cost_usd: null });
  });

  it("sends a new model, turns, and cost", () => {
    const draft = toDraft(agent());
    draft.model = "gpt-6.2-sol";
    draft.maxTurns = "20";
    draft.maxCost = "1.25";
    expect(diffDraft(agent(), draft)).toEqual({ model: "gpt-6.2-sol", max_turns: 20, max_cost_usd: "1.25" });
  });

  it("makes the body of a create with the workspace only for the scope workspace", () => {
    expect(toCreateBody(valid(), "workspace", "w1")).toMatchObject({ scope: "workspace", workspace_id: "w1" });
    expect(toCreateBody(valid(), "user", "w1")).not.toHaveProperty("workspace_id");
    expect(toCreateBody(valid(), "org", null)).not.toHaveProperty("workspace_id");
  });
});

describe("list", () => {
  const all = [
    agent({ id: "1", name: "reviewer", scope: "user" }),
    agent({ id: "2", name: "writer", description: "Writes blog posts", scope: "workspace" }),
    agent({ id: "3", name: "analyst", scope: "org" }),
  ];

  it("filters by scope and by words", () => {
    expect(filterAgents(all, null, "").map((a) => a.id)).toEqual(["1", "2", "3"]);
    expect(filterAgents(all, "org", "").map((a) => a.id)).toEqual(["3"]);
    expect(filterAgents(all, null, "blog WRITES").map((a) => a.id)).toEqual(["2"]);
    expect(filterAgents(all, "user", "blog")).toEqual([]);
  });

  it("writes the limits, the users, and the versions", () => {
    expect(limitsText(agent())).toBe("8 turns, 0.50 USD");
    expect(limitsText(agent({ max_turns: 1, max_cost_usd: null }))).toBe("1 turn");
    expect(limitsText(agent({ max_turns: null, max_cost_usd: null }))).toBeNull();
    expect(usedByText([])).toBe("No agent uses it.");
    expect(usedByText([{ name: "sama" }, { name: "docs" }])).toBe("Used by sama, docs.");
    expect(versionSource({ source: "create", reverts_version: null })).toBe("Created");
    expect(versionSource({ source: "revert", reverts_version: 2 })).toBe("Reverted to version 2");
    expect(specText(agent())).toMatch(/^name: reviewer\n[\s\S]*model: Same as the run[\s\S]*Find bugs\.$/);
  });
});

describe("agent profile", () => {
  it("finds the names that a profile cannot add", () => {
    const all = [agent({ id: "1", name: "a" }), agent({ id: "2", name: "b" })];
    expect([...takenNames(all, ["1"], ["inline", " "])].sort()).toEqual(["a", "inline"]);
  });

  it("finds the tool groups that the profile lacks", () => {
    expect(missingToolsets(agent(), ["core"])).toEqual(["web"]);
    expect(missingToolsets(agent(), ["core", "web", "x"])).toEqual([]);
  });
});
