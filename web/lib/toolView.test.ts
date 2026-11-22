import { describe, expect, it } from "vitest";
import { bareToolName, elapsedText, latestTodos, outputPreview, shortPath, todoList, toolLabel, toolSubject } from "./toolView";

describe("toolLabel", () => {
  it("drops the MCP prefix and names known tools", () => {
    expect(bareToolName("mcp__sama__web_search")).toBe("web_search");
    expect(bareToolName("mcp__my_server__do_it")).toBe("do_it");
    expect(toolLabel("mcp__sama__web_search")).toBe("Search the web");
    expect(toolLabel("Bash")).toBe("Run");
    expect(toolLabel("mcp__sama__browser_click")).toBe("Browser: click");
    expect(toolLabel("mcp__sama__custom_thing")).toBe("Custom thing");
  });
});

describe("toolSubject", () => {
  it("picks the main input", () => {
    expect(toolSubject("Bash", { command: "npm   test\n --watch=false", description: "x" })).toBe(
      "npm test --watch=false",
    );
    expect(toolSubject("Read", { file_path: "/home/user/work/repo/src/app/main.py" })).toBe("…/src/app/main.py");
    expect(toolSubject("web_search", { query: "python release" })).toBe("python release");
    expect(toolSubject("shell", { command: ["bash", "-lc", "ls -la"] })).toBe("ls -la");
    expect(toolSubject("thing", { only: "value" })).toBe("value");
    expect(toolSubject("thing", { a: 1, b: true })).toBe("");
    expect(toolSubject("Bash", { command: "x".repeat(200) })).toHaveLength(96);
  });

  it("keeps a short path", () => {
    expect(shortPath("a/b/c")).toBe("a/b/c");
  });
});

describe("todoList", () => {
  it("reads Claude TodoWrite and Codex update_plan", () => {
    expect(
      todoList("TodoWrite", {
        todos: [
          { content: "Read", status: "completed" },
          { content: "Fix", status: "in_progress" },
          { content: "Test" },
          { nope: 1 },
        ],
      }),
    ).toEqual([
      { text: "Read", status: "completed" },
      { text: "Fix", status: "in_progress" },
      { text: "Test", status: "pending" },
    ]);
    expect(todoList("update_plan", { plan: [{ step: "One", status: "completed" }] })).toEqual([
      { text: "One", status: "completed" },
    ]);
    expect(todoList("Bash", { todos: [] })).toBeNull();
  });

  it("merges the sama todo tool by id", () => {
    const calls = [
      { name: "mcp__sama__todo", arguments: { todos: [{ id: "a", content: "Read" }, { id: "b", content: "Fix" }], merge: false } },
      { name: "mcp__sama__todo", arguments: { todos: [{ id: "a", status: "completed" }, { id: "c", content: "Test" }], merge: true } },
      { name: "mcp__sama__todo", arguments: { todos: null, merge: false } },
    ];
    expect(latestTodos(calls)).toEqual([
      { text: "Read", status: "completed" },
      { text: "Fix", status: "pending" },
      { text: "Test", status: "pending" },
    ]);
  });

  it("finds the newest list of a run", () => {
    const calls = [
      { name: "TodoWrite", arguments: { todos: [{ content: "old" }] } },
      { name: "TodoWrite", arguments: { todos: [{ content: "new" }] } },
      { name: "Bash", arguments: {} },
    ];
    expect(latestTodos(calls)).toEqual([{ text: "new", status: "pending" }]);
    expect(latestTodos([])).toBeNull();
  });
});

describe("small helpers", () => {
  it("formats times and previews", () => {
    expect(elapsedText(8_400)).toBe("8s");
    expect(elapsedText(65_000)).toBe("1m 05s");
    expect(outputPreview("  ")).toBeNull();
    expect(outputPreview("abc", 2)).toBe("ab…");
  });
});
