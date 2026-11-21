// Progress of sub-agents in a run. A `subagent_event` run event carries one line of progress.
// The functions here are pure: they fold the events into groups, one group for each sub-agent
// invocation, so that the chat can show the groups as nested progress.

import { OPEN_RUN_STATUSES, type TraceStep } from "./api";

/** One `subagent_event` of a run, with the fields that the view needs. */
export interface SubagentEvent {
  seq: number;
  subagent: string;
  /** One line of progress. Empty if the event has no text. */
  text: string;
  /** The id of the tool call that started the sub-agent, or `null`. */
  callId: string | null;
  /** `true` on the last event of an invocation. */
  done: boolean;
  tokens: number | null;
}

/** The events of one sub-agent invocation. */
export interface SubagentGroup {
  /** Stable key for lists: it comes from the first event. */
  key: string;
  subagent: string;
  callId: string | null;
  /** The sequence number of the first event: the place of the group in the run. */
  firstSeq: number;
  /** The progress lines in order. Events without text add no step. */
  steps: { seq: number; text: string }[];
  /** `true` if an event has `done`, or if the run ended. */
  done: boolean;
  /** The tokens of the last event that knew them. */
  tokens: number | null;
}

/**
 * The event that a payload describes, or `null` if the payload is not valid (no sub-agent
 * name). The text is plain text and the function does not change it.
 */
export function parseSubagentEvent(seq: number, payload: Record<string, unknown>): SubagentEvent | null {
  const name = typeof payload.subagent === "string" ? payload.subagent.trim() : "";
  if (name === "") return null;
  const callId = typeof payload.call_id === "string" && payload.call_id !== "" ? payload.call_id : null;
  const tokens =
    typeof payload.tokens === "number" && Number.isFinite(payload.tokens) && payload.tokens >= 0
      ? payload.tokens
      : null;
  return {
    seq,
    subagent: name,
    text: typeof payload.text === "string" ? payload.text.trim() : "",
    callId,
    done: payload.done === true,
    tokens,
  };
}

/** Adds an event to a list, in order of `seq`. An event with a known `seq` changes nothing. */
export function addSubagentEvent(events: SubagentEvent[], event: SubagentEvent): SubagentEvent[] {
  if (events.some((known) => known.seq === event.seq)) return events;
  return [...events, event].sort((a, b) => a.seq - b.seq);
}

/**
 * Groups the events of one run. Events with a `callId` group by that id. Events without one
 * group by sub-agent name; such a group closes at its `done` event, and a later event of the
 * same name opens a new group. `runEnded` marks every group as done.
 */
export function groupSubagents(events: SubagentEvent[], runEnded = false): SubagentGroup[] {
  const groups: SubagentGroup[] = [];
  const byCall = new Map<string, SubagentGroup>();
  const openByName = new Map<string, SubagentGroup>();
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    let group = event.callId !== null ? byCall.get(event.callId) : openByName.get(event.subagent);
    if (group === undefined) {
      group = {
        key: `g${event.seq}`,
        subagent: event.subagent,
        callId: event.callId,
        firstSeq: event.seq,
        steps: [],
        done: runEnded,
        tokens: null,
      };
      groups.push(group);
      if (event.callId !== null) byCall.set(event.callId, group);
      else openByName.set(event.subagent, group);
    }
    if (event.text !== "") group.steps.push({ seq: event.seq, text: event.text });
    if (event.tokens !== null) group.tokens = event.tokens;
    if (event.done) {
      group.done = true;
      if (event.callId === null) openByName.delete(event.subagent);
    }
  }
  return groups;
}

/** The groups that belong to a tool call of the run, and the groups that stand alone. */
export interface SubagentPlacement {
  /** The groups of each tool call, by `callId` of the call. */
  nested: Map<string, SubagentGroup[]>;
  /** The groups without a matching tool call, in order of their first event. */
  own: SubagentGroup[];
}

/** Puts each group under its tool call, if the run shows a call with the same id. */
export function placeSubagents(groups: SubagentGroup[], callIds: readonly string[]): SubagentPlacement {
  const known = new Set(callIds);
  const nested = new Map<string, SubagentGroup[]>();
  const own: SubagentGroup[] = [];
  for (const group of groups) {
    if (group.callId !== null && known.has(group.callId)) {
      nested.set(group.callId, [...(nested.get(group.callId) ?? []), group]);
    } else {
      own.push(group);
    }
  }
  return { nested, own };
}

/** `true` if the live status of a run is known and the run is not open. */
export function runHasEnded(status: string | undefined): boolean {
  return status !== undefined && !OPEN_RUN_STATUSES.has(status);
}

/** The events of a stored trace, for the same grouping. */
export function traceSubagentEvents(steps: TraceStep[]): SubagentEvent[] {
  const events: SubagentEvent[] = [];
  for (const step of steps) {
    if (step.type !== "subagent_event") continue;
    const event = parseSubagentEvent(step.seq, step.payload);
    if (event !== null) events.push(event);
  }
  return events;
}

/** The label of a group state, and the summary of the steps, as short text. */
export function subagentStepsLabel(count: number): string {
  return count === 1 ? "1 step" : `${count} steps`;
}

/** The name of a sub-agent for people: "Claude Code" and "Codex" for the coding agents (M7.2). */
export function subagentName(subagent: string): string {
  return Object.hasOwn(CODING_AGENT_NAMES, subagent)
    ? CODING_AGENT_NAMES[subagent as keyof typeof CODING_AGENT_NAMES]
    : subagent;
}

const CODING_AGENT_NAMES = { claude_code: "Claude Code", codex: "Codex" } as const;
