// The menus of the message box, as in Claude Code and Codex: `/` for commands and skills, `@`
// for files, sessions, and links, and the arrow keys for the earlier messages. Pure, so tests
// can run it.

export interface MenuItem {
  /** Unique in the menu. */
  key: string;
  /** What the menu shows, for example `/plan`. */
  label: string;
  /** A short help text. */
  hint: string;
  /** The text that replaces the typed token. */
  insert: string;
  /** A group title, for example "Commands" or "Skills". */
  group: string;
}

/** The commands of the web chat. The API reads the other `/name` texts as skills. */
export const COMMANDS: readonly { name: string; args: string; hint: string }[] = [
  { name: "plan", args: "TASK", hint: "Plan first. The agent changes no files until you approve." },
  { name: "bg", args: "TASK", hint: "Start a second run at the same time." },
  { name: "stop", args: "[NUMBER]", hint: "Stop all runs, or one run." },
  { name: "steer", args: "NOTE", hint: "Add a note to the run that works now." },
  { name: "claude", args: "[TASK]", hint: "Send to Claude Code. Alone: every message goes to Claude Code." },
  { name: "codex", args: "[TASK]", hint: "Send to Codex. Alone: every message goes to Codex." },
  { name: "sama", args: "", hint: "Talk to sama again after /claude or /codex." },
  { name: "compact", args: "[FOCUS]", hint: "Shorten the context of the session." },
  { name: "undo", args: "", hint: "Restore the files from before the last run." },
  { name: "rollback", args: "[NUMBER]", hint: "List the checkpoints, or restore one." },
  { name: "handoff", args: "telegram|slack", hint: "Move the session to a chat app." },
];

const MAX_ITEMS = 8;

/** The command items for a query (the text after `/`). */
export function commandItems(skills: readonly { name: string; description: string }[]): MenuItem[] {
  const commands = COMMANDS.map((c) => ({
    key: `cmd-${c.name}`,
    label: `/${c.name}${c.args ? ` ${c.args}` : ""}`,
    hint: c.hint,
    insert: c.args ? `/${c.name} ` : `/${c.name}`,
    group: "Commands",
  }));
  const reserved = new Set(COMMANDS.map((c) => c.name));
  const skillItems = skills
    .filter((s) => !reserved.has(s.name))
    .map((s) => ({
      key: `skill-${s.name}`,
      label: `/${s.name}`,
      hint: s.description,
      insert: `/${s.name} `,
      group: "Skills",
    }));
  return [...commands, ...skillItems];
}

/** The token at the caret that opens a menu, or `null`. */
export interface Trigger {
  kind: "slash" | "mention";
  /** The index of the `/` or `@`. */
  start: number;
  /** The text after the `/` or `@`, up to the caret. */
  query: string;
}

/**
 * The menu trigger at the caret. `/` opens the command menu only at the start of the text,
 * before any space. `@` opens the mention menu at the start of the text or after a space.
 */
export function triggerAt(text: string, caret: number): Trigger | null {
  const before = text.slice(0, caret);
  const slash = /^\/([\w-]*)$/.exec(before);
  if (slash) return { kind: "slash", start: 0, query: slash[1] ?? "" };
  const mention = /(^|\s)@([^\s@]*)$/.exec(before);
  if (mention) {
    const query = mention[2] ?? "";
    return { kind: "mention", start: caret - query.length - 1, query };
  }
  return null;
}

/** The items that match a query: the label starts with it first, then the label or hint has it. */
export function filterItems(items: readonly MenuItem[], query: string, max = MAX_ITEMS): MenuItem[] {
  const q = query.toLowerCase();
  if (!q) return items.slice(0, max);
  const name = (item: MenuItem) => item.label.replace(/^[/@]/, "").toLowerCase();
  const starts = items.filter((item) => name(item).startsWith(q));
  const has = items.filter(
    (item) => !starts.includes(item) && (name(item).includes(q) || item.hint.toLowerCase().includes(q)),
  );
  return [...starts, ...has].slice(0, max);
}

/** The text after a choice in a menu: the token from `start` to the caret becomes `insert`. */
export function applyChoice(text: string, caret: number, start: number, insert: string): { text: string; caret: number } {
  const after = text.slice(caret);
  const glue = insert.endsWith(" ") || after.startsWith(" ") || after === "" ? "" : " ";
  const next = text.slice(0, start) + insert + glue + after;
  return { text: next, caret: start + insert.length + glue.length };
}

/** The work folder of a session sandbox. `@file:` paths are relative to it (architecture §6.4). */
export const WORK_FOLDER = "/home/user/work/";

/** A path that `@file:` accepts, relative to the work folder, or `null`. */
export function relativeWorkPath(path: string): string | null {
  let rel = path.trim();
  if (rel.startsWith(WORK_FOLDER)) rel = rel.slice(WORK_FOLDER.length);
  if (!rel || rel.startsWith("/") || rel.startsWith("~") || rel.includes("..") || rel.includes("\\")) return null;
  if (/\s/.test(rel)) return null;
  return rel;
}

/** The mention items: files that the agent used in this session, sessions, and the forms. */
export function mentionItems(
  files: readonly string[],
  sessions: readonly { id: string; title: string }[],
): MenuItem[] {
  const fileItems = [...new Set(files.map(relativeWorkPath).filter((p): p is string => p !== null))].map((path) => ({
    key: `file-${path}`,
    label: `@${path}`,
    hint: "File in the session sandbox",
    insert: `@file:${path} `,
    group: "Files",
  }));
  const sessionItems = sessions.map((s) => ({
    key: `session-${s.id}`,
    label: `@${s.title}`,
    hint: "Session: sama reads it for this message",
    insert: `@session:${s.id} `,
    group: "Sessions",
  }));
  const forms: MenuItem[] = [
    { key: "form-file", label: "@file:", hint: "A file of the sandbox, for example @file:src/app.py", insert: "@file:", group: "Other" },
    { key: "form-url", label: "@url:", hint: "A web page, for example @url:https://example.com", insert: "@url:", group: "Other" },
  ];
  return [...fileItems, ...sessionItems, ...forms];
}

/** The file paths in the arguments of tool calls (Read, Edit, Write, and others). */
export function filesOfCalls(calls: readonly { arguments: Record<string, unknown> }[]): string[] {
  const out: string[] = [];
  for (const call of calls) {
    for (const key of ["file_path", "path", "notebook_path"]) {
      const value = call.arguments[key];
      if (typeof value === "string" && value.trim()) out.push(value.trim());
    }
  }
  return out;
}

/**
 * The message box history, as the up arrow of a shell. `index` is `null` when the box shows
 * the draft; else it points into `entries` (oldest first).
 */
export interface HistoryState {
  index: number | null;
  /** The draft that the person typed before the first step back. */
  draft: string;
}

export const historyStart: HistoryState = { index: null, draft: "" };

/** One step back (`up`) or forward (`down`). `null`: no change. */
export function stepHistory(
  entries: readonly string[],
  state: HistoryState,
  direction: "up" | "down",
  current: string,
): { state: HistoryState; text: string } | null {
  if (entries.length === 0) return null;
  if (direction === "up") {
    if (state.index === null) {
      const index = entries.length - 1;
      return { state: { index, draft: current }, text: entries[index] ?? "" };
    }
    if (state.index === 0) return null;
    const index = state.index - 1;
    return { state: { ...state, index }, text: entries[index] ?? "" };
  }
  if (state.index === null) return null;
  if (state.index >= entries.length - 1) return { state: historyStart, text: state.draft };
  const index = state.index + 1;
  return { state: { ...state, index }, text: entries[index] ?? "" };
}

/** The earlier messages of the person for the history: newest last, no repeats in a row. */
export function historyEntries(texts: readonly string[], max = 50): string[] {
  const out: string[] = [];
  for (const text of texts) {
    // The API adds a line to a skill command (architecture §9.5): the box shows what the person typed.
    const t = text.replace(/\n?\(Skill command: [^\n]*\)\s*$/, "").trim();
    if (!t || out[out.length - 1] === t) continue;
    out.push(t);
  }
  return out.slice(-max);
}
