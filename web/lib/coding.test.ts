import { describe, expect, it } from "vitest";
import type { CommandOut, DiffFile, PlanOut } from "./api";
import {
  CHECKPOINTS_CHANGED,
  checkpointWhat,
  commandCounts,
  commandStatusLabel,
  diffChoiceLabel,
  diffRange,
  diffSummary,
  diffTotals,
  fileCounts,
  fileStatusLabel,
  fileStatusTone,
  fileTitle,
  firstLine,
  LARGE_DIFF_LINES,
  listView,
  MAX_PLAN_EDIT_CHARS,
  MAX_PLAN_FEEDBACK_CHARS,
  MAX_PLAN_TASK_CHARS,
  olderPlans,
  optionalText,
  parsePatch,
  parsePlanCommand,
  planCommandError,
  planStatusText,
  planTextChanged,
  proposedPlan,
  pullRequestTitle,
  repoDetail,
  repoStatusLabel,
  restoreTarget,
  safeUrl,
  shortSha,
  startsCollapsed,
  targetStillValid,
  truncateText,
  undoTarget,
} from "./coding";

const PATCH = [
  "diff --git a/src/app.py b/src/app.py",
  "index 1111111..2222222 100644",
  "--- a/src/app.py",
  "+++ b/src/app.py",
  "@@ -1,4 +1,5 @@ def main():",
  " import os",
  "-print('a')",
  "-- a removed line that starts with two dashes",
  "+print('b')",
  "+print('c')",
  " ",
  " end",
  "\\ No newline at end of file",
].join("\n");

function file(over: Partial<DiffFile> = {}): DiffFile {
  return {
    path: "a.py",
    old_path: null,
    status: "modified",
    additions: 1,
    deletions: 1,
    binary: false,
    patch: "",
    truncated: false,
    ...over,
  };
}

function plan(over: Partial<PlanOut> = {}): PlanOut {
  return {
    id: "p1",
    version: 2,
    body: "1. Do it",
    proposed_body: "1. Do it",
    status: "proposed",
    edited: false,
    feedback: null,
    run_id: null,
    decided_at: null,
    created_at: "2026-11-21T09:30:00Z",
    ...over,
  };
}

describe("parsePatch", () => {
  it("reads a hunk with line numbers and kinds", () => {
    const rows = parsePatch(PATCH);
    expect(rows.map((r) => [r.kind, r.oldNo, r.newNo, r.text])).toEqual([
      ["hunk", null, null, "@@ -1,4 +1,5 @@ def main():"],
      ["context", 1, 1, "import os"],
      ["del", 2, null, "print('a')"],
      ["del", 3, null, "- a removed line that starts with two dashes"],
      ["add", null, 2, "print('b')"],
      ["add", null, 3, "print('c')"],
      ["context", 4, 4, ""],
      ["context", 5, 5, "end"],
      ["note", null, null, "No newline at end of file"],
    ]);
  });

  it("reads two hunks and a header with no counts", () => {
    const rows = parsePatch(["diff --git a/x b/x", "@@ -1 +1 @@", "-a", "+b", "@@ -10,2 +10,2 @@", " c", "-d", "+e"].join("\n"));
    expect(rows.filter((r) => r.kind === "hunk")).toHaveLength(2);
    expect(rows.find((r) => r.text === "e")).toMatchObject({ kind: "add", newNo: 11 });
    expect(rows.find((r) => r.text === "d")).toMatchObject({ kind: "del", oldNo: 11 });
  });

  it("reads a new file and ignores the file headers of the next file", () => {
    const rows = parsePatch(
      ["diff --git a/n b/n", "new file mode 100644", "--- /dev/null", "+++ b/n", "@@ -0,0 +1,2 @@", "+one", "+two", "diff --git a/m b/m", "--- a/m", "+++ b/m", "@@ -1 +1 @@", "-x", "+y"].join("\n"),
    );
    expect(rows.map((r) => r.kind)).toEqual(["hunk", "add", "add", "hunk", "del", "add"]);
  });

  it("gives no rows for a binary or empty patch and keeps rows of a cut patch", () => {
    expect(parsePatch("")).toEqual([]);
    expect(parsePatch("diff --git a/i.png b/i.png\nBinary files differ\n")).toEqual([]);
    const cut = parsePatch(["diff --git a/x b/x", "@@ -1,9 +1,9 @@", "-a", "+b"].join("\n"));
    expect(cut.map((r) => r.kind)).toEqual(["hunk", "del", "add"]);
  });

  it("accepts CRLF and an empty context line without a space", () => {
    const rows = parsePatch("@@ -1,2 +1,2 @@\r\n a\r\n\r\n");
    expect(rows.map((r) => [r.kind, r.text])).toEqual([
      ["hunk", "@@ -1,2 +1,2 @@"],
      ["context", "a"],
      ["context", ""],
    ]);
  });
});

describe("file helpers", () => {
  it("labels a status", () => {
    expect(fileStatusLabel("added")).toBe("Added");
    expect(fileStatusLabel("type_changed")).toBe("Type changed");
    expect(fileStatusLabel("copied")).toBe("copied");
    expect(fileStatusTone("added")).toBe("ok");
    expect(fileStatusTone("deleted")).toBe("error");
    expect(fileStatusTone("renamed")).toBe("wait");
  });

  it("shows counts, totals, and titles", () => {
    expect(fileCounts(file({ additions: 3, deletions: 0 }))).toBe("+3 -0");
    expect(fileCounts(file({ additions: null, deletions: null, binary: true }))).toBe("Binary file");
    expect(diffTotals([file({ additions: 3, deletions: 1 }), file({ additions: null, deletions: null, binary: true })])).toEqual({
      additions: 3,
      deletions: 1,
    });
    expect(fileTitle(file({ status: "renamed", old_path: "old.py", path: "new.py" }))).toBe("old.py → new.py");
    expect(fileTitle(file())).toBe("a.py");
  });

  it("collapses a large diff", () => {
    expect(startsCollapsed(file({ additions: LARGE_DIFF_LINES, deletions: 1 }))).toBe(true);
    expect(startsCollapsed(file({ additions: 5, deletions: 5 }))).toBe(false);
    expect(startsCollapsed(file({ additions: null, deletions: null, binary: true }))).toBe(false);
  });
});

describe("diff range", () => {
  it("maps a choice to base and head", () => {
    expect(diffRange("all")).toEqual({ base: "oldest", head: null });
    expect(diffRange(1)).toEqual({ base: 1, head: null });
    expect(diffRange(7)).toEqual({ base: 7, head: 6 });
  });

  it("names the choices and the compared points", () => {
    const item = { number: 7, commit: "c".repeat(40), created_at: "2026-11-21T09:30:00Z", tool: "Edit", kind: "tool" };
    expect(diffChoiceLabel(item)).toBe("Step 7: before Edit");
    expect(checkpointWhat({ tool: "", kind: "rollback" })).toBe("the files before a restore");
    expect(diffSummary(item, null)).toBe("From checkpoint 7 (before Edit) to the current files.");
    expect(diffSummary(item, { ...item, number: 6, tool: "Bash" })).toBe(
      "From checkpoint 7 (before Edit) to checkpoint 6 (before Bash).",
    );
  });
});

describe("parsePlanCommand", () => {
  it("reads every form", () => {
    expect(parsePlanCommand("/plan")).toEqual({ kind: "status" });
    expect(parsePlanCommand("  /plan  ")).toEqual({ kind: "status" });
    expect(parsePlanCommand("/plan add a retry to the fetch")).toEqual({ kind: "start", task: "add a retry to the fetch" });
    expect(parsePlanCommand("/plan approve")).toEqual({ kind: "approve" });
    expect(parsePlanCommand("/PLAN Approve")).toEqual({ kind: "approve" });
    expect(parsePlanCommand("/plan edit 1. A\n2. B")).toEqual({ kind: "edit", text: "1. A\n2. B" });
    expect(parsePlanCommand("/plan edit")).toEqual({ kind: "edit", text: "" });
    expect(parsePlanCommand("/plan reject")).toEqual({ kind: "reject", feedback: "" });
    expect(parsePlanCommand("/plan reject too big, split it")).toEqual({ kind: "reject", feedback: "too big, split it" });
    expect(parsePlanCommand("/plan on")).toEqual({ kind: "mode", on: true });
    expect(parsePlanCommand("/plan OFF")).toEqual({ kind: "mode", on: false });
  });

  it("treats a word with more text as a task, and other text as no command", () => {
    expect(parsePlanCommand("/plan approve the new design of the API")).toEqual({
      kind: "start",
      task: "approve the new design of the API",
    });
    expect(parsePlanCommand("/plan on the main page add a button")).toEqual({
      kind: "start",
      task: "on the main page add a button",
    });
    expect(parsePlanCommand("/planning")).toBeNull();
    expect(parsePlanCommand("please /plan")).toBeNull();
    expect(parsePlanCommand("hello")).toBeNull();
  });
});

describe("plans", () => {
  it("finds the plan that waits and the older ones", () => {
    expect(proposedPlan(plan())).not.toBeNull();
    expect(proposedPlan(plan({ status: "approved" }))).toBeNull();
    expect(proposedPlan(null)).toBeNull();
    expect(olderPlans([plan({ id: "a" }), plan({ id: "b" }), plan({ id: "c" })]).map((p) => p.id)).toEqual(["b", "c"]);
    expect(olderPlans([])).toEqual([]);
  });

  it("tells when an edit changes the plan", () => {
    expect(planTextChanged(plan(), "1. Do it \n")).toBe(false);
    expect(planTextChanged(plan(), "1. Do more")).toBe(true);
    expect(planTextChanged(plan(), "   ")).toBe(false);
  });

  it("writes the status text of /plan", () => {
    expect(planStatusText({ plan_mode: false, plan: null })).toBe(
      "Plan mode is off. The session has no plan yet. Send /plan TASK to start one.",
    );
    expect(planStatusText({ plan_mode: true, plan: plan() })).toContain("/plan approve");
    expect(planStatusText({ plan_mode: true, plan: plan({ status: "rejected" }) })).toBe(
      "Plan mode is on. The agent proposes a plan and does not change files. Plan 2: rejected.",
    );
  });
});

describe("commands, repos, and pull requests", () => {
  const cmd = (over: Partial<CommandOut>): CommandOut => ({
    run_id: "r",
    call_id: "c",
    tool: "Bash",
    command: "pytest",
    test: true,
    status: "ok",
    output: "",
    summary: null,
    started_at: "2026-11-21T09:30:00Z",
    finished_at: null,
    ...over,
  });

  it("labels the status of a command", () => {
    expect(commandStatusLabel(cmd({}))).toBe("passed");
    expect(commandStatusLabel(cmd({ test: false }))).toBe("ok");
    expect(commandStatusLabel(cmd({ status: "failed" }))).toBe("failed");
    expect(commandStatusLabel(cmd({ status: "running" }))).toBe("running");
    expect(commandCounts([cmd({}), cmd({ test: false })])).toBe("1 test run of 2 commands");
  });

  it("describes a repo", () => {
    expect(repoStatusLabel({ status: "pending" })).toBe("Clones at the next run");
    expect(repoDetail({ ref: null, branch: "main", commit: "a".repeat(40), proxied: true })).toBe(
      "main · aaaaaaa · can push sama/* branches",
    );
    expect(repoDetail({ ref: "v1", branch: null, commit: null, proxied: null })).toBe("v1");
    expect(shortSha(null)).toBe("");
  });

  it("titles a pull request and checks its link", () => {
    expect(pullRequestTitle({ title: " Fix ", repo: "o/n", head: "sama/x" })).toBe("Fix");
    expect(pullRequestTitle({ title: null, repo: "o/n", head: "sama/x" })).toBe("sama/x");
    expect(pullRequestTitle({ title: null, repo: null, head: null })).toBe("Pull request");
    expect(safeUrl("https://github.com/o/n/pull/1")).toBe("https://github.com/o/n/pull/1");
    expect(safeUrl("javascript:alert(1)")).toBeNull();
    expect(safeUrl("not a url")).toBeNull();
    expect(safeUrl(null)).toBeNull();
  });

  it("allows only a GitHub pull request link", () => {
    expect(safeUrl("https://github.com/my-org/my.repo_1/pull/123")).not.toBeNull();
    expect(safeUrl("https://github.com.attacker.example/o/n/pull/1")).toBeNull();
    expect(safeUrl("https://attacker.example/github.com/o/n/pull/1")).toBeNull();
    expect(safeUrl("https://notgithub.com/o/n/pull/1")).toBeNull();
    expect(safeUrl("https://www.github.com/o/n/pull/1")).toBeNull();
    expect(safeUrl("http://github.com/o/n/pull/1")).toBeNull();
    expect(safeUrl("https://user@github.com/o/n/pull/1")).toBeNull();
    expect(safeUrl("https://user:pass@github.com/o/n/pull/1")).toBeNull();
    expect(safeUrl("https://github.com:8443/o/n/pull/1")).toBeNull();
    expect(safeUrl("https://github.com@attacker.example/o/n/pull/1")).toBeNull();
    expect(safeUrl("https://github.com/o/n/issues/1")).toBeNull();
    expect(safeUrl("https://github.com/o/n/pull/x")).toBeNull();
    expect(safeUrl("https://github.com/o/n/pull/1/files")).toBeNull();
    expect(safeUrl("https://github.com/o/pull/1")).toBeNull();
  });
});

describe("text helpers", () => {
  it("trims, cuts, and takes the first line", () => {
    expect(optionalText("  ")).toBeUndefined();
    expect(optionalText(" a ")).toBe("a");
    expect(truncateText("abcdef", 4)).toBe("abc…");
    expect(truncateText("abc", 4)).toBe("abc");
    expect(firstLine("\n\n  Title here \nmore")).toBe("Title here");
    expect(firstLine("")).toBe("");
  });
});

describe("plan command limits", () => {
  const edit = (n: number) => parsePlanCommand(`/plan edit ${"a".repeat(n)}`)!;
  const reject = (n: number) => parsePlanCommand(`/plan reject ${"a".repeat(n)}`)!;
  const start = (n: number) => parsePlanCommand(`/plan ${"a".repeat(n)}`)!;

  it("checks the edited plan at its boundary", () => {
    expect(planCommandError(edit(MAX_PLAN_EDIT_CHARS))).toBeNull();
    expect(planCommandError(edit(MAX_PLAN_EDIT_CHARS + 1))).toBe(
      "The edited plan is too long. Use 20,000 characters or less.",
    );
    expect(planCommandError({ kind: "edit", text: "" })).toContain("/plan edit");
  });

  it("checks the feedback at its boundary", () => {
    expect(planCommandError(reject(MAX_PLAN_FEEDBACK_CHARS))).toBeNull();
    expect(planCommandError(reject(MAX_PLAN_FEEDBACK_CHARS + 1))).toBe(
      "The feedback is too long. Use 4,000 characters or less.",
    );
    expect(planCommandError({ kind: "reject", feedback: "" })).toBeNull();
  });

  it("checks the task at its boundary and ignores other commands", () => {
    expect(planCommandError(start(MAX_PLAN_TASK_CHARS))).toBeNull();
    expect(planCommandError(start(MAX_PLAN_TASK_CHARS + 1))).toBe("The task is too long. Use 32,000 characters or less.");
    expect(planCommandError({ kind: "approve" })).toBeNull();
    expect(planCommandError({ kind: "status" })).toBeNull();
  });
});

describe("listView", () => {
  it("shows an error before the empty state, and keeps the data", () => {
    expect(listView(null, null)).toEqual({ kind: "loading", warning: null });
    expect(listView(null, "down")).toEqual({ kind: "error", warning: null });
    expect(listView([], null)).toEqual({ kind: "empty", warning: null });
    expect(listView([], "down")).toEqual({ kind: "empty", warning: "down" });
    expect(listView([1], "down")).toEqual({ kind: "list", warning: "down" });
    expect(listView([1], null)).toEqual({ kind: "list", warning: null });
  });
});

describe("restore targets", () => {
  const cp = (number: number, commit: string) => ({
    number,
    commit: commit.repeat(40),
    created_at: "2026-11-21T09:30:00Z",
    tool: "Edit",
    kind: "tool",
  });

  it("keeps a restore while the checkpoint has the same number and commit", () => {
    const items = [cp(1, "a"), cp(2, "b")];
    const target = restoreTarget(items[1]!);
    expect(target).toEqual({ kind: "restore", number: 2, commit: "b".repeat(40) });
    expect(targetStillValid(target, items)).toBe(true);
  });

  it("ends the confirm step when a new checkpoint shifts the numbers", () => {
    const target = restoreTarget(cp(2, "b"));
    // A new checkpoint came: the old 2 is now 3.
    expect(targetStillValid(target, [cp(1, "n"), cp(2, "a"), cp(3, "b")])).toBe(false);
  });

  it("ends the confirm step when the checkpoint is gone", () => {
    expect(targetStillValid(restoreTarget(cp(2, "b")), [cp(1, "a")])).toBe(false);
    expect(targetStillValid(restoreTarget(cp(2, "b")), [])).toBe(false);
  });

  it("ends an undo when the newest checkpoint changes", () => {
    const target = undoTarget([cp(1, "a"), cp(2, "b")]);
    expect(targetStillValid(target, [cp(1, "a"), cp(2, "b")])).toBe(true);
    expect(targetStillValid(target, [cp(1, "n"), cp(2, "a")])).toBe(false);
    expect(CHECKPOINTS_CHANGED).toContain("Look at the list again");
  });
});

