// Helpers of the memory editor (docs/architecture.md, Section 8). The API checks every rule
// again; these checks only give the user a fast answer.

import type { MemoryTarget } from "./api";

/** The text between two entries (src/sama/memory/core.py `ENTRY_DELIMITER`). */
export const ENTRY_DELIMITER = "\n§\n";

export const TARGET_TITLES: Record<MemoryTarget, string> = {
  agent: "Agent notes",
  user: "User profile",
  workspace: "Workspace notes",
};

export const TARGET_HELP: Record<MemoryTarget, string> = {
  agent: "What this agent knows about the work. Only this agent reads them.",
  user: "What the agents know about you. Only you can see them.",
  workspace: "Notes that every agent of the workspace reads. A workspace admin changes them.",
};

/**
 * The characters that the entries use for the cap, delimiters included. Like the API, it counts
 * Unicode code points, not UTF-16 units.
 */
export function memoryUsed(entries: readonly string[]): number {
  return Array.from(entries.join(ENTRY_DELIMITER)).length;
}

/** The entries to save: trimmed, without empty entries, without duplicates. */
export function cleanEntries(entries: readonly string[]): string[] {
  return [...new Set(entries.map((e) => e.trim()).filter((e) => e !== ""))];
}

/** The problem of the cleaned entries, or `null` if the API can take them. */
export function entriesProblem(entries: readonly string[], cap: number): string | null {
  if (entries.some((e) => e.includes("§"))) return "An entry cannot contain the character §.";
  const used = memoryUsed(entries);
  if (used > cap) return `The notes use ${used} of ${cap} characters. Remove ${used - cap} characters.`;
  return null;
}
