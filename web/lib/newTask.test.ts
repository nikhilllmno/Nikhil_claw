import { describe, expect, it } from "vitest";
import { startPlan, taskTitle } from "./newTask";

describe("taskTitle", () => {
  it("takes the first line and cuts it at a space", () => {
    expect(taskTitle("  Fix the login bug\nmore detail")).toBe("Fix the login bug");
    expect(taskTitle("   ")).toBeNull();
    const long = "Add a retry to every fetch call in the client and write tests for the retry rules of the API";
    const title = taskTitle(long) ?? "";
    expect(title.length).toBeLessThanOrEqual(80);
    expect(title.endsWith("…")).toBe(true);
    expect(title).not.toMatch(/\s…$/);
  });
});

describe("startPlan", () => {
  it("chooses the first step", () => {
    expect(startPlan(" Fix it ", " owner/repo ", " dev ", true)).toEqual({
      repo: { repo: "owner/repo", ref: "dev" },
      first: "plan",
      task: "Fix it",
    });
    expect(startPlan("Hi", "", "main", false)).toEqual({ repo: null, first: "message", task: "Hi" });
    expect(startPlan("", "owner/repo", "", true)).toEqual({ repo: { repo: "owner/repo" }, first: "none", task: "" });
  });
});
