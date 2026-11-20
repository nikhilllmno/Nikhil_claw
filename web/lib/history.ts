// Helpers of the session search, the export, and the run traces (docs/architecture.md,
// Section 11.4).

import type { RunSummary, TraceStep } from "./api";

/** A cost in USD as text. The API sends a decimal string. */
export function formatCost(usd: string | number): string {
  const value = typeof usd === "number" ? usd : Number.parseFloat(usd);
  if (!Number.isFinite(value) || value === 0) return "$0";
  if (value < 0.0001) return "<$0.0001";
  return `$${value < 1 ? value.toFixed(4) : value.toFixed(2)}`;
}

/** A token count as short text: 950, 1.2k, 3.4M. */
export function formatTokens(count: number): string {
  if (count < 1000) return String(count);
  if (count < 1_000_000) return `${(count / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return `${(count / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
}

/** The time between two ISO times as short text, or "" if one is missing. */
export function formatDuration(start: string | null, end: string | null): string {
  if (start === null || end === null) return "";
  const ms = Date.parse(end) - Date.parse(start);
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `${ms} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1).replace(/\.0$/, "")} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds - minutes * 60)} s`;
}

export type Tone = "ok" | "wait" | "error" | "off";

export function runTone(status: string): Tone {
  if (status === "completed") return "ok";
  if (status === "queued" || status === "running" || status === "paused") return "wait";
  if (status === "failed" || status === "error") return "error";
  return "off";
}

/** The tone of a chat app status (`pending`, `active`, `failed`). `undefined`: not connected. */
export function integrationTone(status: string | undefined): Tone {
  if (status === undefined) return "off";
  if (status === "active") return "ok";
  if (status === "pending") return "wait";
  return "error";
}

const TONE_BADGES: Record<Tone, string> = {
  ok: "badge badge-accent",
  wait: "badge badge-warning",
  error: "badge badge-danger",
  off: "badge",
};

/** The global badge classes of a tone. */
export function toneBadge(tone: Tone): string {
  return TONE_BADGES[tone];
}

/** The tone of a tool call status (`started`, `finished`, `failed`, `denied`). */
export function toolTone(status: string): Tone {
  if (status === "finished") return "ok";
  if (status === "started") return "wait";
  if (status === "failed" || status === "denied") return "error";
  return "off";
}

const STEP_TITLES: Record<string, string> = {
  text: "Answer",
  tool_call_started: "Tool call",
  tool_call_finished: "Tool result",
  approval_required: "Approval needed",
  run_started: "Run started",
  run_completed: "Run completed",
  run_failed: "Run failed",
};

/** The title of a trace step. */
export function stepTitle(step: TraceStep): string {
  return STEP_TITLES[step.type] ?? step.type.replace(/_/g, " ");
}

/** One line about a trace step, or "" if the step has nothing short to show. */
export function stepSummary(step: TraceStep): string {
  const p = step.payload;
  const text = (value: unknown) => (typeof value === "string" ? value : "");
  switch (step.type) {
    case "text":
      return text(p.text);
    case "tool_call_started":
      return text(p.name) || text(p.tool_name);
    case "tool_call_finished":
      return `${text(p.name) || text(p.call_id)}${p.is_error === true ? " (error)" : ""}`;
    case "run_failed":
      return [text(p.code), text(p.message)].filter(Boolean).join(": ");
    default:
      return "";
  }
}

/** A part of a search snippet. `match` is true for the words that the search found. */
export interface SnippetPart {
  text: string;
  match: boolean;
}

const SNIPPET_MARK = /<<(.*?)>>/gs;

/**
 * Splits a search snippet at the markers `<<` and `>>` of the API (src/sama/search/core.py).
 * The result has no markers. A marker without its pair stays as text.
 */
export function snippetParts(snippet: string): SnippetPart[] {
  const parts: SnippetPart[] = [];
  let last = 0;
  for (const found of snippet.matchAll(SNIPPET_MARK)) {
    if (found.index > last) parts.push({ text: snippet.slice(last, found.index), match: false });
    if (found[1] !== "") parts.push({ text: found[1] ?? "", match: true });
    last = found.index + found[0].length;
  }
  if (last < snippet.length) parts.push({ text: snippet.slice(last), match: false });
  return parts;
}

/** The input and output tokens of a run as one line. */
export function runUsage(run: RunSummary): string {
  return `${formatTokens(run.input_tokens)} in · ${formatTokens(run.output_tokens)} out · ${formatCost(run.cost_usd)}`;
}

/** Saves a file in the browser. */
export function saveFile(name: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
