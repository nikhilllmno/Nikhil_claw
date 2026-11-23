// The live browser panel (M8.3), as Claude for Chrome and the browser view of Codex show it: the
// step of the agent on top, the live view, and who has control. Pure, so tests can run it.

import type { LiveState } from "./chat";
import { bareToolName, toolLabel, toolSubject } from "./toolView";

export const BROWSER_TASK = "browser_task";
/** Load a new live view URL for the same tab after this time (the server gives 10 minutes). */
export const VIEW_REFRESH_MS = 8 * 60_000;
/** Ask the server for the live browser this often while a run is open. */
export const POLL_MS = 3_000;

function isBrowserTool(name: string): boolean {
  return /^browser_/.test(bareToolName(name));
}

/**
 * The current step of the agent in the browser of a run, for the line on top of the panel, or
 * `null`. A running `browser_task` shows the newest step of its sub-agent.
 */
export function browserStep(live: Pick<LiveState, "tools" | "subagents">, runId: string): string | null {
  const calls = (live.tools[runId] ?? []).filter((call) => isBrowserTool(call.name));
  const last = calls[calls.length - 1];
  if (!last) return null;
  if (bareToolName(last.name) === BROWSER_TASK) {
    const steps = (live.subagents[runId] ?? []).filter(
      (event) => event.subagent === BROWSER_TASK && event.text && (event.callId === null || event.callId.startsWith(BROWSER_TASK)),
    );
    const step = steps[steps.length - 1];
    if (last.status === "running" && step) return `Browser task: ${step.text}`;
  }
  const subject = toolSubject(last.name, last.arguments);
  const label = toolLabel(last.name);
  const text = subject ? `${label}: ${subject}` : label;
  return last.status === "running" ? text : `${text} (done)`;
}

/** The host of a page URL for the address line, or the URL when it does not parse. */
export function shortUrl(url: string | null | undefined): string {
  if (!url) return "";
  try {
    const parsed = new URL(url);
    return `${parsed.host}${parsed.pathname === "/" ? "" : parsed.pathname}`;
  } catch {
    return url;
  }
}
