import { describe, expect, it } from "vitest";
import {
  applyChoice,
  commandItems,
  filesOfCalls,
  filterItems,
  historyEntries,
  historyStart,
  mentionItems,
  relativeWorkPath,
  stepHistory,
  triggerAt,
} from "./commands";

describe("triggerAt", () => {
  it("opens the command menu only at the start", () => {
    expect(triggerAt("/pl", 3)).toEqual({ kind: "slash", start: 0, query: "pl" });
    expect(triggerAt("/", 1)).toEqual({ kind: "slash", start: 0, query: "" });
    expect(triggerAt("/plan fix", 9)).toBeNull();
    expect(triggerAt("a /plan", 7)).toBeNull();
  });

  it("opens the mention menu after a space or at the start", () => {
    expect(triggerAt("look at @src", 12)).toEqual({ kind: "mention", start: 8, query: "src" });
    expect(triggerAt("@", 1)).toEqual({ kind: "mention", start: 0, query: "" });
    expect(triggerAt("mail@example", 12)).toBeNull();
    expect(triggerAt("@a b", 4)).toBeNull();
  });
});

describe("menus", () => {
  it("lists commands, then skills that are not commands", () => {
    const items = commandItems([
      { name: "code-review", description: "Review code" },
      { name: "plan", description: "dup" },
    ]);
    expect(items.find((i) => i.label === "/code-review")?.group).toBe("Skills");
    expect(items.filter((i) => i.label.startsWith("/plan"))).toHaveLength(1);
    expect(items.find((i) => i.key === "cmd-plan")?.insert).toBe("/plan ");
    expect(items.find((i) => i.key === "cmd-undo")?.insert).toBe("/undo");
  });

  it("filters by start, then by text", () => {
    const items = commandItems([{ name: "codebase-map", description: "Map a repo" }]);
    expect(filterItems(items, "co").map((i) => i.key).slice(0, 3)).toEqual(["cmd-codex", "cmd-compact", "skill-codebase-map"]);
    expect(filterItems(items, "repo").map((i) => i.key)).toEqual(["skill-codebase-map"]);
    expect(filterItems(items, "", 3)).toHaveLength(3);
  });

  it("replaces the token with the choice", () => {
    expect(applyChoice("/pl", 3, 0, "/plan ")).toEqual({ text: "/plan ", caret: 6 });
    expect(applyChoice("read @ma and", 8, 5, "@file:main.py ")).toEqual({ text: "read @file:main.py  and", caret: 19 });
    expect(applyChoice("x @u", 4, 2, "@url:")).toEqual({ text: "x @url:", caret: 7 });
  });

  it("builds mentions from files and sessions", () => {
    const files = filesOfCalls([
      { arguments: { file_path: "/home/user/work/repo/a.py" } },
      { arguments: { path: "../etc/passwd" } },
      { arguments: { file_path: "/home/user/work/repo/a.py" } },
    ]);
    const items = mentionItems(files, [{ id: "s1", title: "Old chat" }]);
    expect(items.map((i) => i.insert)).toEqual(["@file:repo/a.py ", "@session:s1 ", "@file:", "@url:"]);
  });

  it("accepts only safe work paths", () => {
    expect(relativeWorkPath("/home/user/work/x/y.md")).toBe("x/y.md");
    expect(relativeWorkPath("/etc/passwd")).toBeNull();
    expect(relativeWorkPath("a b.txt")).toBeNull();
  });
});

describe("history", () => {
  it("steps back and forward and keeps the draft", () => {
    const entries = ["one", "two"];
    const up1 = stepHistory(entries, historyStart, "up", "draft");
    expect(up1?.text).toBe("two");
    const up2 = stepHistory(entries, up1!.state, "up", "two");
    expect(up2?.text).toBe("one");
    expect(stepHistory(entries, up2!.state, "up", "one")).toBeNull();
    const down1 = stepHistory(entries, up2!.state, "down", "one");
    expect(down1?.text).toBe("two");
    const down2 = stepHistory(entries, down1!.state, "down", "two");
    expect(down2).toEqual({ state: historyStart, text: "draft" });
    expect(stepHistory(entries, historyStart, "down", "")).toBeNull();
    expect(stepHistory([], historyStart, "up", "")).toBeNull();
  });

  it("drops repeats and the skill line", () => {
    expect(historyEntries(["a", "a", " ", "/review x\n(Skill command: load the skill \"review\" with skill_view.)"])).toEqual([
      "a",
      "/review x",
    ]);
  });
});
