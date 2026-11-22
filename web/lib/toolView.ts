// How the chat shows one tool call in one line, as Claude Code and Codex do: a short verb and
// the main input (the command, the file, the URL, or the query). Pure, so tests can run it.

/** The prefix that the Claude runtime puts before the name of a tool of an MCP server. */
const MCP_PREFIX = /^mcp__[^_]+(?:_[^_]+)*?__/;

const LABELS: Record<string, string> = {
  bash: "Run",
  shell: "Run",
  exec_command: "Run",
  local_shell: "Run",
  read: "Read",
  write: "Write",
  edit: "Edit",
  multiedit: "Edit",
  apply_patch: "Edit",
  notebookedit: "Edit notebook",
  glob: "Find files",
  grep: "Search code",
  ls: "List files",
  webfetch: "Fetch",
  websearch: "Search the web",
  web_search: "Search the web",
  web_extract: "Read web page",
  x_search: "Search X",
  todowrite: "Update the to-do list",
  todo: "Update the to-do list",
  update_plan: "Update the to-do list",
  task: "Sub-agent",
  delegate_task: "Delegate",
  memory: "Memory",
  memory_recall: "Recall memory",
  session_search: "Search sessions",
  skill_view: "Read skill",
  skills_list: "List skills",
  clarify: "Ask you",
  plan_propose: "Propose a plan",
  coding_agent_run: "Coding agent",
  pr_create: "Open a pull request",
  repo_clone: "Clone repo",
  vision_analyze: "Look at image",
  video_analyze: "Watch video",
  image_generate: "Make image",
  tool_search: "Find tools",
  browser_task: "Browser task",
  moa: "Ask several models",
};

/** The keys of the arguments that hold the main input of a call, in order of preference. */
const SUBJECT_KEYS = [
  "command",
  "cmd",
  "file_path",
  "path",
  "notebook_path",
  "url",
  "urls",
  "query",
  "q",
  "pattern",
  "repo",
  "name",
  "description",
  "task",
  "prompt",
  "content",
  "action",
] as const;

const MAX_SUBJECT = 96;

/** The name of a tool without the MCP prefix, for example `web_search` for `mcp__sama__web_search`. */
export function bareToolName(name: string): string {
  return name.replace(MCP_PREFIX, "");
}

/** A short verb for a tool, for example "Run" for `Bash` and "Search the web" for `web_search`. */
export function toolLabel(name: string): string {
  const bare = bareToolName(name);
  const known = LABELS[bare.toLowerCase()];
  if (known) return known;
  if (/^browser_/i.test(bare)) return `Browser: ${bare.slice("browser_".length).replace(/_/g, " ")}`;
  const words = bare.replace(/[_-]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Tool";
}

function oneLine(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function cut(text: string, max = MAX_SUBJECT): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** A path with only its last three parts, so that the file name stays visible. */
export function shortPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  if (parts.length <= 3) return path;
  return `…/${parts.slice(-3).join("/")}`;
}

/** The main input of a call in one line, or `""` when no argument is a good subject. */
export function toolSubject(name: string, args: Record<string, unknown>): string {
  for (const key of SUBJECT_KEYS) {
    const value = args[key];
    let text: string | null = null;
    if (typeof value === "string") text = value;
    else if (Array.isArray(value) && value.every((part) => typeof part === "string")) {
      // A shell call of the OpenAI runtime gives the command as a list: `["bash", "-lc", "ls"]`.
      const parts = value as string[];
      text =
        parts.length >= 3 && /^(ba)?sh$/.test(parts[0] ?? "") && parts[1] === "-lc"
          ? parts.slice(2).join(" ")
          : parts.join(key === "urls" ? ", " : " ");
    }
    if (text === null) continue;
    const line = oneLine(text);
    if (!line) continue;
    if (key === "file_path" || key === "path" || key === "notebook_path") return cut(shortPath(line));
    return cut(line);
  }
  // A tool with one short string argument: show it.
  const strings = Object.values(args).filter((v): v is string => typeof v === "string" && v.trim() !== "");
  if (strings.length === 1) return cut(oneLine(strings[0] ?? ""));
  void name;
  return "";
}

/** The output preview of a call, cut for the row. `null`: nothing to show. */
export function outputPreview(preview: string, max = 2_000): string | null {
  const text = preview.trim();
  if (!text) return null;
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/** One item of a to-do list of the agent (Claude `TodoWrite`, Codex `update_plan`). */
export interface TodoItem {
  text: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
}

function todoStatus(value: unknown): TodoItem["status"] {
  return value === "completed" || value === "in_progress" || value === "cancelled" ? value : "pending";
}

type Call = { name: string; arguments: Record<string, unknown> };

/** The items of a to-do call, with the id of each item when it has one. `null`: not a to-do call. */
function todoChanges(call: Call): { replace: boolean; items: (Partial<TodoItem> & { id: string | null })[] } | null {
  const bare = bareToolName(call.name).toLowerCase();
  if (bare !== "todowrite" && bare !== "update_plan" && bare !== "todo") return null;
  const args = call.arguments;
  const raw = Array.isArray(args.todos) ? args.todos : Array.isArray(args.plan) ? args.plan : null;
  // The sama `todo` tool with no todos reads the list.
  if (raw === null) return bare === "todo" ? { replace: false, items: [] } : null;
  const items: (Partial<TodoItem> & { id: string | null })[] = [];
  for (const entry of raw) {
    if (entry === null || typeof entry !== "object") continue;
    const record = entry as Record<string, unknown>;
    const text = [record.content, record.step, record.text, record.title].find((v) => typeof v === "string");
    const id = typeof record.id === "string" ? record.id : null;
    const item: Partial<TodoItem> & { id: string | null } = { id };
    if (typeof text === "string" && text.trim()) item.text = oneLine(text);
    if (record.status !== undefined && record.status !== null) item.status = todoStatus(record.status);
    if (item.text === undefined && id === null) continue;
    items.push(item);
  }
  // `todo` with merge=true changes items by id; the other tools give the whole list.
  return { replace: !(bare === "todo" && args.merge === true), items };
}

/** The to-do list in the arguments of one to-do call (Claude `TodoWrite`, Codex `update_plan`, sama `todo`). */
export function todoList(name: string, args: Record<string, unknown>): TodoItem[] | null {
  const changes = todoChanges({ name, arguments: args });
  if (changes === null || changes.items.length === 0) return null;
  return changes.items
    .filter((item) => item.text !== undefined)
    .map((item) => ({ text: item.text as string, status: item.status ?? "pending" }));
}

/**
 * The to-do list after all to-do calls, in order. A full list replaces the list; a merge of the
 * sama `todo` tool changes the items with the same id and adds new ones. `null`: no call.
 */
export function latestTodos(calls: readonly Call[]): TodoItem[] | null {
  let list: (TodoItem & { id: string | null })[] | null = null;
  for (const call of calls) {
    const changes = todoChanges(call);
    if (changes === null || (changes.items.length === 0 && !changes.replace)) continue;
    if (changes.replace || list === null) {
      list = changes.items
        .filter((item) => item.text !== undefined)
        .map((item) => ({ id: item.id, text: item.text as string, status: item.status ?? "pending" }));
      continue;
    }
    for (const item of changes.items) {
      const index = item.id === null ? -1 : list.findIndex((known) => known.id === item.id);
      if (index >= 0) {
        const known = list[index];
        if (known) list[index] = { ...known, text: item.text ?? known.text, status: item.status ?? known.status };
      } else if (item.text !== undefined) {
        list.push({ id: item.id, text: item.text, status: item.status ?? "pending" });
      }
    }
  }
  return list === null ? null : list.map(({ text, status }) => ({ text, status }));
}

/** The text of a duration in seconds, for example "8s" or "1m 05s". */
export function elapsedText(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
}
