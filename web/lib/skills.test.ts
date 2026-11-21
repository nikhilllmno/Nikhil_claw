import { describe, expect, it } from "vitest";
import type { SkillSummary } from "./api";
import { curatorBadge, filterSkills, nameProblem, pathProblem, skillTemplate } from "./skills";

function skill(name: string, description: string, tags: string[] = []): SkillSummary {
  return {
    id: null,
    name,
    description,
    scope: "builtin",
    version: "1",
    tags,
    can_edit: false,
    status: "active",
    pinned: false,
  };
}

describe("skill helpers", () => {
  it("checks the name like the lint", () => {
    expect(nameProblem("deploy-check")).toBeNull();
    expect(nameProblem("")).toBe("Give a name.");
    expect(nameProblem("Deploy")).toMatch(/lowercase/);
    expect(nameProblem("a--b")).toMatch(/lowercase/);
    expect(nameProblem("a".repeat(65))).toMatch(/64/);
  });

  it("checks the file path", () => {
    expect(pathProblem("references/api.md")).toBeNull();
    expect(pathProblem("api.md")).toMatch(/folders/);
    expect(pathProblem("other/api.md")).toMatch(/folders/);
    expect(pathProblem("scripts/../x")).toMatch(/letters/);
  });

  it("makes a template with the name", () => {
    expect(skillTemplate("x")).toMatch(/^---\nname: x\n/);
  });

  it("filters on all words", () => {
    const all = [skill("summarize", "Summarize a text", ["writing"]), skill("deploy", "Ship code")];
    expect(filterSkills(all, "")).toHaveLength(2);
    expect(filterSkills(all, "text writ").map((s) => s.name)).toEqual(["summarize"]);
    expect(filterSkills(all, "nothing")).toEqual([]);
  });

  it("shows one curator badge: pinned wins over stale", () => {
    expect(curatorBadge({ status: "active", pinned: false })).toBeNull();
    expect(curatorBadge({ status: "stale", pinned: false })).toEqual({ label: "Stale", tone: "warning" });
    expect(curatorBadge({ status: "stale", pinned: true })).toEqual({ label: "Pinned", tone: "accent" });
    expect(curatorBadge({ status: "active", pinned: true })).toEqual({ label: "Pinned", tone: "accent" });
  });
});
