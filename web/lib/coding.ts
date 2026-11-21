// Coding mode (M7.9): pure helpers for the Code page and for the `/plan` command of the composer.
// The components call these functions, so tests can run them.

import type {
  CheckpointItem,
  CodingOut,
  CommandOut,
  DiffFile,
  DiffPoint,
  FileStatus,
  PlanOut,
  PlanStatus,
  PullRequestOut,
  RepoOut,
} from "./api";
import type { Tone } from "./history";

// -- unified diff -------------------------------------------------------------------------------

export type DiffLineKind = "hunk" | "add" | "del" | "context" | "note";

/** One row of a rendered diff. A `hunk` row is the `@@ ... @@` header. */
export interface DiffLine {
  kind: DiffLineKind;
  /** The text of the line without the `+`, `-`, or space at its start. */
  text: string;
  /** The line number in the old file. `null` for an added line and for a header. */
  oldNo: number | null;
  /** The line number in the new file. `null` for a removed line and for a header. */
  newNo: number | null;
}

const HUNK_HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

/**
 * Reads the patch of one file (unified diff text) into rows. The lines before the first hunk
 * (`diff --git`, `index`, `---`, `+++`) are not rows. The counts in each hunk header decide where
 * the hunk ends, so a removed line such as `-- a comment` is not mistaken for a file header.
 */
export function parsePatch(patch: string): DiffLine[] {
  const rows: DiffLine[] = [];
  let oldLeft = 0;
  let newLeft = 0;
  let oldNo = 0;
  let newNo = 0;
  const lines = patch.split("\n");
  // A patch that ends with a newline gives one empty piece at the end.
  if (lines.at(-1) === "") lines.pop();
  for (const raw of lines) {
    const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    const header = HUNK_HEADER.exec(line);
    if (header) {
      oldNo = Number(header[1]);
      newNo = Number(header[3]);
      oldLeft = header[2] === undefined ? 1 : Number(header[2]);
      newLeft = header[4] === undefined ? 1 : Number(header[4]);
      rows.push({ kind: "hunk", text: line, oldNo: null, newNo: null });
      continue;
    }
    // A file header ends an open hunk (a truncated patch can leave counts above zero).
    if (line.startsWith("diff --git ")) {
      oldLeft = 0;
      newLeft = 0;
      continue;
    }
    if (line.startsWith("\\")) {
      // "\ No newline at end of file" belongs to the line before it.
      const last = rows.at(-1);
      if (last !== undefined && last.kind !== "hunk" && last.kind !== "note") {
        rows.push({ kind: "note", text: line.replace(/^\\ ?/, ""), oldNo: null, newNo: null });
      }
      continue;
    }
    if (oldLeft <= 0 && newLeft <= 0) continue; // Outside a hunk: a file header line.
    if (line.startsWith("+")) {
      rows.push({ kind: "add", text: line.slice(1), oldNo: null, newNo });
      newNo += 1;
      newLeft -= 1;
    } else if (line.startsWith("-")) {
      rows.push({ kind: "del", text: line.slice(1), oldNo, newNo: null });
      oldNo += 1;
      oldLeft -= 1;
    } else {
      // A context line. Some tools remove the single space of an empty context line.
      rows.push({ kind: "context", text: line.startsWith(" ") ? line.slice(1) : line, oldNo, newNo });
      oldNo += 1;
      newNo += 1;
      oldLeft -= 1;
      newLeft -= 1;
    }
  }
  return rows;
}

/** A file whose diff has more changed lines than this starts collapsed. */
export const LARGE_DIFF_LINES = 200;

/** True when the diff of a file starts collapsed. */
export function startsCollapsed(file: DiffFile): boolean {
  return (file.additions ?? 0) + (file.deletions ?? 0) > LARGE_DIFF_LINES;
}

const FILE_STATUS_LABELS: Record<FileStatus, string> = {
  added: "Added",
  modified: "Modified",
  deleted: "Deleted",
  renamed: "Renamed",
  type_changed: "Type changed",
};

/** The label of a file status, for example `Added`. An unknown status shows as it is. */
export function fileStatusLabel(status: string): string {
  return FILE_STATUS_LABELS[status as FileStatus] ?? status;
}

/** The tone of the badge of a file status. */
export function fileStatusTone(status: string): Tone {
  if (status === "added") return "ok";
  if (status === "deleted") return "error";
  if (status === "modified") return "off";
  return "wait";
}

/** The name of a file in the list. A renamed file shows `old -> new`. */
export function fileTitle(file: Pick<DiffFile, "path" | "old_path" | "status">): string {
  return file.status === "renamed" && file.old_path ? `${file.old_path} → ${file.path}` : file.path;
}

/** The counts of a file, for example `+3 -1`. A binary file has no counts. */
export function fileCounts(file: Pick<DiffFile, "additions" | "deletions" | "binary">): string {
  if (file.binary || file.additions === null || file.deletions === null) return "Binary file";
  return `+${file.additions} -${file.deletions}`;
}

/** The sums of the counts of all files. A binary file adds nothing. */
export function diffTotals(files: readonly DiffFile[]): { additions: number; deletions: number } {
  let additions = 0;
  let deletions = 0;
  for (const file of files) {
    additions += file.additions ?? 0;
    deletions += file.deletions ?? 0;
  }
  return { additions, deletions };
}

/** What a diff compares, for example `Checkpoint 7 (before Edit) to the current files`. */
export function diffSummary(base: DiffPoint, head: DiffPoint | null): string {
  const from = `checkpoint ${base.number} (${checkpointWhat(base)})`;
  const to = head === null ? "the current files" : `checkpoint ${head.number} (${checkpointWhat(head)})`;
  return `From ${from} to ${to}.`;
}

/** The choice of the diff view: all changes, or the step of one checkpoint. */
export type DiffChoice = "all" | number;

/**
 * The `base` and `head` of the diff for a choice. "All changes" is `base=oldest` and no head.
 * Checkpoint N is the files before a tool call, so the step of N is `base=N`, `head=N-1`. The
 * newest step (N is 1) has no head: it runs to the current files.
 */
export function diffRange(choice: DiffChoice): { base: number | "oldest"; head: number | null } {
  if (choice === "all") return { base: "oldest", head: null };
  return { base: choice, head: choice > 1 ? choice - 1 : null };
}

/** The text of one choice in the diff menu. */
export function diffChoiceLabel(item: CheckpointItem): string {
  return `Step ${item.number}: ${checkpointWhat(item)}`;
}

// -- checkpoints --------------------------------------------------------------------------------

/** What a checkpoint holds, for example `before Edit`. */
export function checkpointWhat(item: Pick<CheckpointItem, "tool" | "kind">): string {
  if (item.kind === "rollback") return "the files before a restore";
  return item.tool ? `before ${item.tool}` : "before a tool call";
}

/** What a section with a polled list shows. */
export interface ListView {
  kind: "loading" | "error" | "empty" | "list";
  /** A failed poll. With data, the data stays on screen and this is a warning with a retry. */
  warning: string | null;
}

/**
 * Chooses the view of a polled list. A failed poll shows before the empty state, so that an
 * empty list is never the answer to an error. The last good data stays.
 */
export function listView(data: readonly unknown[] | null, error: string | null): ListView {
  if (data === null) return error ? { kind: "error", warning: null } : { kind: "loading", warning: null };
  return { kind: data.length === 0 ? "empty" : "list", warning: error };
}

// -- restore ------------------------------------------------------------------------------------

/** What waits for a confirmation in the checkpoint list. */
export type RestoreTarget =
  | { kind: "undo"; newest: string | null }
  | { kind: "restore"; number: number; commit: string };

/** The target of Undo: it depends on the newest checkpoint. */
export function undoTarget(items: readonly CheckpointItem[]): RestoreTarget {
  return { kind: "undo", newest: items[0]?.commit ?? null };
}

/** The target of Restore for one checkpoint: its number and its stable commit. */
export function restoreTarget(item: Pick<CheckpointItem, "number" | "commit">): RestoreTarget {
  return { kind: "restore", number: item.number, commit: item.commit };
}

/**
 * True when the confirmed target is still what the user looked at. A new poll can shift the
 * numbers: then the commit is gone or has another number, and the confirm step must end.
 */
export function targetStillValid(target: RestoreTarget, items: readonly CheckpointItem[]): boolean {
  if (target.kind === "undo") return (items[0]?.commit ?? null) === target.newest;
  return items.some((i) => i.number === target.number && i.commit === target.commit);
}

export const CHECKPOINTS_CHANGED = "The checkpoints changed since you looked at them. Look at the list again.";

// -- plans --------------------------------------------------------------------------------------

const PLAN_STATUS_LABELS: Record<PlanStatus, string> = {
  proposed: "Waiting for you",
  approved: "Approved",
  rejected: "Rejected",
  superseded: "Replaced",
};

export function planStatusLabel(status: string): string {
  return PLAN_STATUS_LABELS[status as PlanStatus] ?? status;
}

export function planTone(status: string): Tone {
  if (status === "proposed") return "wait";
  if (status === "approved") return "ok";
  if (status === "rejected") return "error";
  return "off";
}

/** The plan if it waits for a decision, else `null`. Only the newest plan can be proposed. */
export function proposedPlan(plan: PlanOut | null | undefined): PlanOut | null {
  return plan?.status === "proposed" ? plan : null;
}

/** The older versions of a plan list: every plan except the newest one. */
export function olderPlans(plans: readonly PlanOut[]): PlanOut[] {
  return plans.slice(1);
}

/** The plan text that a person may edit: the current body. */
export function planEditText(plan: PlanOut): string {
  return plan.body;
}

/** True when the text differs from the plan after a trim. An empty text is not a plan. */
export function planTextChanged(plan: PlanOut, text: string): boolean {
  return text.trim() !== "" && text.trim() !== plan.body.trim();
}

// -- plan command -------------------------------------------------------------------------------

/** The `/plan` command of the composer (and of Telegram and Slack). */
export type PlanCommand =
  | { kind: "status" }
  | { kind: "start"; task: string }
  | { kind: "approve" }
  | { kind: "edit"; text: string }
  | { kind: "reject"; feedback: string }
  | { kind: "mode"; on: boolean };

/**
 * The plan command in a composer text, or `null` if the text is not one. The words `approve`,
 * `on`, and `off` are commands only when they are alone. Any other text after `/plan` is the
 * task. `/plan edit` without text gives `{ kind: "edit", text: "" }`, so that the composer can
 * show the usage.
 */
export function parsePlanCommand(text: string): PlanCommand | null {
  const match = /^\/plan(?:\s+([\s\S]*))?$/i.exec(text.trim());
  if (!match) return null;
  const rest = (match[1] ?? "").trim();
  if (rest === "") return { kind: "status" };
  const word = /^(\S+)(?:\s+([\s\S]*))?$/.exec(rest);
  const first = (word?.[1] ?? "").toLowerCase();
  const after = (word?.[2] ?? "").trim();
  if (after === "") {
    if (first === "approve") return { kind: "approve" };
    if (first === "on") return { kind: "mode", on: true };
    if (first === "off") return { kind: "mode", on: false };
  }
  if (first === "edit") return { kind: "edit", text: after };
  if (first === "reject") return { kind: "reject", feedback: after };
  return { kind: "start", task: rest };
}

/** The limits of the API for the text of the plan routes. */
export const MAX_PLAN_TASK_CHARS = 32_000;
export const MAX_PLAN_EDIT_CHARS = 20_000;
export const MAX_PLAN_FEEDBACK_CHARS = 4_000;

/**
 * The problem of a plan command that the API would not take, as text for the user, or `null`.
 * `/plan edit` needs a text, and each text has its own limit.
 */
export function planCommandError(command: PlanCommand): string | null {
  const tooLong = (what: string, max: number) =>
    `${what} is too long. Use ${max.toLocaleString("en-US")} characters or less.`;
  if (command.kind === "edit") {
    if (command.text === "") return PLAN_EDIT_USAGE;
    if (command.text.length > MAX_PLAN_EDIT_CHARS) return tooLong("The edited plan", MAX_PLAN_EDIT_CHARS);
  }
  if (command.kind === "reject" && command.feedback.length > MAX_PLAN_FEEDBACK_CHARS) {
    return tooLong("The feedback", MAX_PLAN_FEEDBACK_CHARS);
  }
  if (command.kind === "start" && command.task.length > MAX_PLAN_TASK_CHARS) {
    return tooLong("The task", MAX_PLAN_TASK_CHARS);
  }
  return null;
}

export const PLAN_EDIT_USAGE = "Write the new plan after the command, for example: /plan edit 1. Add a test. 2. Fix the bug.";
export const NO_PROPOSED_PLAN = "No plan waits for a decision.";

/** The text of `/plan` without words: plan mode and the newest plan. */
export function planStatusText(coding: Pick<CodingOut, "plan_mode" | "plan">): string {
  const mode = coding.plan_mode
    ? "Plan mode is on. The agent proposes a plan and does not change files."
    : "Plan mode is off.";
  const plan = coding.plan;
  if (plan === null) return `${mode} The session has no plan yet. Send /plan TASK to start one.`;
  const state = `Plan ${plan.version}: ${planStatusLabel(plan.status).toLowerCase()}.`;
  if (plan.status === "proposed") {
    return `${mode} ${state} Send /plan approve, /plan edit TEXT, or /plan reject. Or open the Code page.`;
  }
  return `${mode} ${state}`;
}

/** The text that the chat shows after a reject without feedback. */
export const PLAN_REJECTED_NOTICE = "Plan rejected. Plan mode stays on. Send /plan TASK to ask for a new plan.";

/** The text that the chat shows after `/plan on` or `/plan off`. */
export function planModeNotice(on: boolean): string {
  return on
    ? "Plan mode is on. The agent proposes a plan before it changes files."
    : "Plan mode is off. The agent can change files.";
}

// -- commands and tests -------------------------------------------------------------------------

/** The label of the status badge of a command. A test run shows `passed` or `failed`. */
export function commandStatusLabel(command: Pick<CommandOut, "status" | "test">): string {
  if (command.status === "running") return "running";
  if (command.status === "failed") return "failed";
  return command.test ? "passed" : "ok";
}

export function commandTone(status: string): Tone {
  if (status === "running") return "wait";
  if (status === "failed") return "error";
  return "ok";
}

/** The counts of a command list for the filter text, for example `2 test runs of 5 commands`. */
export function commandCounts(commands: readonly CommandOut[]): string {
  const tests = commands.filter((c) => c.test).length;
  const word = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
  return `${word(tests, "test run", "test runs")} of ${word(commands.length, "command", "commands")}`;
}

// -- repos --------------------------------------------------------------------------------------

export function repoStatusLabel(repo: Pick<RepoOut, "status">): string {
  if (repo.status === "ready") return "Ready";
  if (repo.status === "failed") return "Failed";
  return "Clones at the next run";
}

export function repoTone(status: string): Tone {
  if (status === "ready") return "ok";
  if (status === "failed") return "error";
  return "wait";
}

/** The most repos that a session can hold. */
export const MAX_REPOS = 5;

/** The first 7 characters of a commit. */
export function shortSha(sha: string | null): string {
  return sha ? sha.slice(0, 7) : "";
}

/** The ref, branch, and commit of a repo in one line, for example `main · 1a2b3c4`. */
export function repoDetail(repo: Pick<RepoOut, "ref" | "branch" | "commit" | "proxied">): string {
  const parts: string[] = [];
  const branch = repo.branch ?? repo.ref;
  if (branch) parts.push(branch);
  if (repo.commit) parts.push(shortSha(repo.commit));
  if (repo.proxied === true) parts.push("can push sama/* branches");
  return parts.join(" · ");
}

// -- pull requests ------------------------------------------------------------------------------

export function pullRequestTone(status: string): Tone {
  if (status === "opened") return "ok";
  if (status === "failed") return "error";
  return "wait";
}

export function pullRequestStatusLabel(status: PullRequestOut["status"] | string): string {
  if (status === "opened") return "opened";
  if (status === "failed") return "failed";
  return "waiting";
}

/** The title of a pull request in the list. */
export function pullRequestTitle(pr: Pick<PullRequestOut, "title" | "repo" | "head">): string {
  return pr.title?.trim() || pr.head || pr.repo || "Pull request";
}

const PULL_PATH = /^\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/\d+$/;

/**
 * The link of a pull request, only if it is `https://github.com/OWNER/REPO/pull/NUMBER`: the host
 * is exactly `github.com`, there is no port or user info, and the path has that form.
 */
export function safeUrl(url: string | null): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    const ok =
      parsed.protocol === "https:" &&
      parsed.hostname === "github.com" &&
      parsed.port === "" &&
      parsed.username === "" &&
      parsed.password === "" &&
      PULL_PATH.test(parsed.pathname);
    return ok ? url : null;
  } catch {
    return null;
  }
}

// -- text ---------------------------------------------------------------------------------------

/** The text without spaces at its ends, or `undefined` if nothing is left. */
export function optionalText(text: string): string | undefined {
  const trimmed = text.trim();
  return trimmed === "" ? undefined : trimmed;
}

/** The text cut to `max` characters. A cut text ends with an ellipsis. */
export function truncateText(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, Math.max(0, max - 1)).trimEnd()}…`;
}

/** The first line of a text that is not empty, cut to `max` characters. */
export function firstLine(text: string, max = 120): string {
  const line = text.split("\n").find((l) => l.trim() !== "") ?? "";
  return truncateText(line.trim(), max);
}
