// Helpers of the skill editor (docs/architecture.md, Section 9). The API lints every write
// again; these checks only give the user a fast answer.

import type { SkillScope, SkillSummary } from "./api";

export const SCOPE_LABELS: Record<SkillScope, string> = {
  builtin: "Built-in",
  org: "Org",
  workspace: "Workspace",
  user: "Only you",
};

/** The folders that a skill file can be in (src/sama/skills/lint.py `FILE_FOLDERS`). */
export const FILE_FOLDERS = ["references", "templates", "scripts", "assets"] as const;

const NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const SEGMENT = /^[A-Za-z0-9._-]+$/;

/** The problem of a skill name, or `null`. */
export function nameProblem(name: string): string | null {
  if (name === "") return "Give a name.";
  if (name.length > 64) return "The name can have 64 characters or fewer.";
  if (!NAME.test(name)) return "Use lowercase letters, digits, and single hyphens, for example deploy-check.";
  return null;
}

/** The problem of a file path of a skill, or `null`. */
export function pathProblem(path: string): string | null {
  const parts = path.split("/");
  if (parts.length < 2 || !(FILE_FOLDERS as readonly string[]).includes(parts[0] ?? "")) {
    return `Put the file in one of these folders: ${FILE_FOLDERS.join(", ")}.`;
  }
  if (parts.slice(1).some((p) => !SEGMENT.test(p) || p === "." || p === "..")) {
    return "Use letters, digits, dots, hyphens, and underscores in the path.";
  }
  return null;
}

/** The first SKILL.md of a new skill. */
export function skillTemplate(name: string): string {
  return `---\nname: ${name || "my-skill"}\ndescription: Says in a few words what the skill does\nversion: 1\n---\n\nWrite the steps here.\n`;
}

/** The skills whose name, description, or tags have all words of `text`. */
export function filterSkills(skills: readonly SkillSummary[], text: string): SkillSummary[] {
  const words = text.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [...skills];
  return skills.filter((s) => {
    const hay = `${s.name} ${s.description} ${s.tags.join(" ")}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

/** The curator badges of a skill (Section 9.7): pinned, or stale. */
export function curatorBadges(skill: Pick<SkillSummary, "status" | "pinned">): { label: string; tone: "accent" | "warning" }[] {
  const badges: { label: string; tone: "accent" | "warning" }[] = [];
  if (skill.pinned) badges.push({ label: "Pinned", tone: "accent" });
  else if (skill.status === "stale") badges.push({ label: "Stale", tone: "warning" });
  return badges;
}

export const STALE_TEXT =
  "No agent used this skill for 30 days, so the curator marked it as stale. It still works. " +
  "If nobody uses it for 90 days, the curator archives it. Use it or pin it to keep it.";
