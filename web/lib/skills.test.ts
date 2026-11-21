import { describe, expect, it } from "vitest";
import type { SkillSettingSpec, SkillSummary } from "./api";
import {
  curatorBadge,
  filterSkills,
  nameProblem,
  pathProblem,
  settingProblem,
  settingsBody,
  settingsChanged,
  settingsForm,
  skillTemplate,
} from "./skills";

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

const specs: SkillSettingSpec[] = [
  { key: "region", type: "string", description: "The region.", default: "eu", required: false },
  { key: "retries", type: "number", description: "The tries.", default: 3, required: false },
  { key: "verbose", type: "boolean", description: "More logs.", default: false, required: false },
  { key: "project", type: "string", description: "The project.", default: null, required: true },
];

describe("skill settings form", () => {
  it("starts from the stored values, and checkboxes from the value in use", () => {
    const form = settingsForm({
      specs,
      values: { retries: 5, verbose: true },
      effective: { region: "eu", retries: 5, verbose: true, project: null },
    });
    expect(form).toEqual({ region: "", retries: "5", verbose: true, project: "" });
    const none = settingsForm({
      specs,
      values: {},
      effective: { region: "eu", retries: 3, verbose: false, project: null },
    });
    expect(none).toEqual({ region: "", retries: "", verbose: false, project: "" });
  });

  it("sends null for an empty field, so the default applies", () => {
    const { values, errors } = settingsBody(specs, { region: " ", retries: "", verbose: false, project: "p" });
    expect(values).toEqual({ region: null, retries: null, verbose: null, project: "p" });
    expect(errors).toEqual({});
  });

  it("sends numbers as numbers and strings as typed", () => {
    const { values } = settingsBody(specs, { region: "us", retries: "2.5", verbose: true, project: "p" });
    expect(values).toEqual({ region: "us", retries: 2.5, verbose: true, project: "p" });
  });

  it("finds a number that is not finite, and a missing required value", () => {
    const { errors } = settingsBody(specs, { region: "", retries: "abc", verbose: false, project: "" });
    expect(errors).toEqual({ retries: "Give a number.", project: "Give a value." });
    expect(settingProblem(specs[1]!, "Infinity")).toBe("Give a number.");
    expect(settingProblem(specs[1]!, "1e3")).toBeNull();
    expect(settingProblem({ type: "string", required: true, default: "x" }, "")).toBeNull();
  });

  it("keeps a stored checkbox value, even if it is the default", () => {
    const form = { region: "", retries: "", verbose: false, project: "p" };
    expect(settingsBody(specs, form, { verbose: false }).values.verbose).toBe(false);
    expect(settingsBody(specs, form).values.verbose).toBeNull();
    expect(settingsBody(specs, { ...form, verbose: true }).values.verbose).toBe(true);
  });

  it("sees a change", () => {
    const saved = { region: "", retries: "", verbose: false, project: "p" };
    expect(settingsChanged(specs, { ...saved }, saved)).toBe(false);
    expect(settingsChanged(specs, { ...saved, retries: "4" }, saved)).toBe(true);
  });
});
