// The new-task form of the home page, as in Codex: a task, an optional repo and branch, and
// "Plan first". Pure, so tests can run it.

export const MAX_TITLE_CHARS = 80;

/** The session title from the task: its first line, cut at a space. `null` for no task. */
export function taskTitle(task: string): string | null {
  const line = task.trim().split("\n")[0]?.trim() ?? "";
  if (!line) return null;
  if (line.length <= MAX_TITLE_CHARS) return line;
  const cut = line.slice(0, MAX_TITLE_CHARS - 1);
  const space = cut.lastIndexOf(" ");
  return `${space > MAX_TITLE_CHARS / 2 ? cut.slice(0, space) : cut}…`;
}

/** The steps of a start: what to call after the session exists. */
export interface StartPlan {
  repo: { repo: string; ref?: string } | null;
  /** `plan`: plan mode with the task; `message`: a normal first message; `none`: no task. */
  first: "plan" | "message" | "none";
  task: string;
}

export function startPlan(task: string, repo: string, branch: string, planFirst: boolean): StartPlan {
  const text = task.trim();
  const repoText = repo.trim();
  const ref = branch.trim();
  return {
    repo: repoText ? { repo: repoText, ...(ref ? { ref } : {}) } : null,
    first: text ? (planFirst ? "plan" : "message") : "none",
    task: text,
  };
}
