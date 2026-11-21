import { describe, expect, it } from "vitest";
import type { ToolInfo, ToolPolicyRule } from "./api";
import {
  checkChangeCount,
  customRules,
  groupTools,
  isPrefixRule,
  newRuleRow,
  orgRuleFor,
  policyActionLabel,
  policyChanges,
  policyChoiceLabel,
  ruleMap,
  tableDraft,
  toRuleRows,
  validateRuleRows,
  validateRuleTool,
  wantedRules,
} from "./policy";

function tool(name: string, toolset: string, enabled = true): ToolInfo {
  return { name, toolset, enabled, default: "ask", always_ask: false };
}

const tools: ToolInfo[] = [
  tool("web_search", "web"),
  tool("web_extract", "web"),
  tool("repo_clone", "github", false),
];

describe("labels", () => {
  it("names the actions and the choices", () => {
    expect(policyActionLabel("allow")).toBe("Allow");
    expect(policyActionLabel("ask")).toBe("Ask");
    expect(policyActionLabel("deny")).toBe("Deny");
    expect(policyChoiceLabel("default")).toBe("Default");
    expect(policyChoiceLabel("deny")).toBe("Deny");
  });
});

describe("rules", () => {
  const rules: ToolPolicyRule[] = [
    { tool: "web_search", action: "allow" },
    { tool: "mcp__github__*", action: "deny" },
    { tool: "other_tool", action: "ask" },
  ];

  it("finds a prefix rule", () => {
    expect(isPrefixRule("mcp__*")).toBe(true);
    expect(isPrefixRule("web_search")).toBe(false);
  });

  it("makes a map", () => {
    expect(ruleMap(rules)).toEqual({ web_search: "allow", "mcp__github__*": "deny", other_tool: "ask" });
  });

  it("keeps the rules that no listed tool owns", () => {
    expect(customRules(rules, tools).map((r) => r.tool)).toEqual(["mcp__github__*", "other_tool"]);
  });

  it("makes the table draft of the listed tools", () => {
    expect(tableDraft(rules, tools)).toEqual({ web_search: "allow" });
  });

  it("groups the tools by toolset", () => {
    const groups = groupTools(tools);
    expect(groups.map((g) => [g.toolset, g.enabled, g.tools.length])).toEqual([
      ["web", true, 2],
      ["github", false, 1],
    ]);
  });
});

describe("orgRuleFor", () => {
  const org: ToolPolicyRule[] = [
    { tool: "web_*", action: "ask" },
    { tool: "web_search_*", action: "deny" },
    { tool: "repo_clone", action: "allow" },
  ];

  it("prefers the exact name", () => {
    expect(orgRuleFor("repo_clone", org)).toEqual({ action: "allow", via: "repo_clone" });
  });

  it("uses the longest prefix", () => {
    expect(orgRuleFor("web_search_x", org)).toEqual({ action: "deny", via: "web_search_*" });
    expect(orgRuleFor("web_extract", org)).toEqual({ action: "ask", via: "web_*" });
  });

  it("gives null when no rule applies", () => {
    expect(orgRuleFor("Bash", org)).toBeNull();
    expect(orgRuleFor("Bash", [])).toBeNull();
  });
});

describe("policyChanges", () => {
  const saved: ToolPolicyRule[] = [
    { tool: "web_search", action: "allow" },
    { tool: "old_*", action: "deny" },
  ];

  it("sends nothing when nothing changed", () => {
    expect(policyChanges(saved, { web_search: "allow", "old_*": "deny" })).toEqual([]);
  });

  it("sends a new rule, a changed rule, and a removed rule", () => {
    expect(policyChanges(saved, { web_search: "deny", repo_clone: "ask" })).toEqual([
      { tool: "old_*", action: null },
      { tool: "repo_clone", action: "ask" },
      { tool: "web_search", action: "deny" },
    ]);
  });

  it("sends nothing for a tool with no rule and no saved rule", () => {
    expect(policyChanges([], {})).toEqual([]);
  });
});

describe("custom rule rows", () => {
  it("makes rows from rules", () => {
    const rows = toRuleRows([{ tool: "a*", action: "deny" }]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ tool: "a*", action: "deny" });
    expect(newRuleRow().action).toBe("ask");
    expect(newRuleRow().uid).not.toBe(newRuleRow().uid);
  });

  it("checks a tool name like the API", () => {
    expect(validateRuleTool("")).toContain("needs a tool name");
    expect(validateRuleTool("a b")).toContain("not valid");
    expect(validateRuleTool("a*b")).toContain("not valid");
    expect(validateRuleTool("x".repeat(201))).toContain("at most 200");
    expect(validateRuleTool("mcp__github__*")).toBeNull();
    expect(validateRuleTool(" web_search ")).toBeNull();
  });

  it("refuses a repeated tool and a tool of the table", () => {
    const rows = [newRuleRow("a*"), newRuleRow(" a* ")];
    expect(validateRuleRows(rows)).toBe("Two rules have the tool a*.");
    expect(validateRuleRows([newRuleRow("web_search")], new Set(["web_search"]))).toContain("in the table");
    expect(validateRuleRows([newRuleRow("a*"), newRuleRow("b*")])).toBeNull();
    expect(validateRuleRows([newRuleRow("")])).toContain("needs a tool name");
  });

  it("joins the table and the rows", () => {
    expect(
      wantedRules({ web_search: "allow", web_extract: "default" }, [newRuleRow(" mcp__* ", "deny")]),
    ).toEqual({ web_search: "allow", "mcp__*": "deny" });
  });
});

describe("checkChangeCount", () => {
  it("limits the changes of one save", () => {
    const changes = Array.from({ length: 21 }, (_, i) => ({ tool: `t${i}`, action: null }));
    expect(checkChangeCount(changes, 20)).toContain("at most 20");
    expect(checkChangeCount(changes.slice(0, 20), 20)).toBeNull();
  });
});
