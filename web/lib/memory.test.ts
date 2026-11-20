import { describe, expect, it } from "vitest";
import { cleanEntries, entriesProblem, memoryUsed } from "./memory";

describe("memory helpers", () => {
  it("counts the delimiters like the API", () => {
    expect(memoryUsed([])).toBe(0);
    expect(memoryUsed(["ab"])).toBe(2);
    expect(memoryUsed(["ab", "cd"])).toBe(7);
  });

  it("trims, drops empty entries, and drops duplicates", () => {
    expect(cleanEntries([" a ", "", "b", "a", "  "])).toEqual(["a", "b"]);
  });

  it("finds the delimiter character and a full cap", () => {
    expect(entriesProblem(["a § b"], 100)).toMatch(/§/);
    expect(entriesProblem(["abcdef"], 5)).toBe("The notes use 6 of 5 characters. Remove 1 characters.");
    expect(entriesProblem(["abc"], 5)).toBeNull();
  });
});
