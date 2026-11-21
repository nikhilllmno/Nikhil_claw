// The live state of one chat session, built from the event stream
// (`GET /sessions/{id}/events`). The functions here are pure, so tests can run them.

import {
  type CheckpointItem,
  OPEN_RUN_STATUSES,
  type RunEventPayload,
  type RunsEvent,
  type SteerNote,
  type StreamRun,
} from "./api";

export interface ToolCall {
  callId: string;
  name: string;
  arguments: Record<string, unknown>;
  /** `running` until the finish event arrives. */
  status: "running" | "done" | "error";
  preview: string;
}

export interface LiveState {
  /** The status of each run that the stream told about. */
  runs: Record<string, StreamRun>;
  /** The streamed text of each run that is open. */
  text: Record<string, string>;
  /** The tool calls of each run, in order. */
  tools: Record<string, ToolCall[]>;
  /** Failure text of a run that ended in this view and failed. */
  failure: string | null;
  /**
   * The partial text of each run that a user stopped in this view. The server keeps no message
   * for a stopped run, so the view keeps the text and marks it as stopped.
   */
  stopped: Record<string, string>;
}

export type Effect = "reload_messages" | "reload_approvals" | "reload_questions";

export const initialLive: LiveState = { runs: {}, text: {}, tools: {}, failure: null, stopped: {} };

export function openRunIds(state: LiveState): string[] {
  return Object.values(state.runs)
    .filter((run) => OPEN_RUN_STATUSES.has(run.status))
    .map((run) => run.run_id);
}

export function isPaused(state: LiveState): boolean {
  return Object.values(state.runs).some((run) => run.status === "paused");
}

function failureText(run: StreamRun): string | null {
  if (run.status === "failed" || run.status === "error") {
    return run.failure_message ? `The run failed: ${run.failure_message}` : "The run failed.";
  }
  return null;
}

/** Ends the live text of a run. The text of a stopped run moves to `stopped`. */
function endRunText(state: LiveState, runId: string, stopped: boolean): LiveState {
  const text = Object.fromEntries(Object.entries(state.text).filter(([id]) => id !== runId));
  if (!stopped || state.stopped[runId] !== undefined) return { ...state, text };
  return { ...state, text, stopped: { ...state.stopped, [runId]: state.text[runId] ?? "" } };
}

/** A run changed its status. `wasOpen`: this view showed the run as open before. */
function applyRun(state: LiveState, run: StreamRun, effects: Effect[]): LiveState {
  const before = state.runs[run.run_id];
  const wasOpen = before !== undefined && OPEN_RUN_STATUSES.has(before.status);
  const next: LiveState = { ...state, runs: { ...state.runs, [run.run_id]: run } };
  if (run.status === "paused") {
    effects.push("reload_approvals");
    return next;
  }
  if (OPEN_RUN_STATUSES.has(run.status)) return { ...next, failure: null };
  if (!wasOpen) return next; // The end of a run that this view never showed as open is old news.
  effects.push("reload_messages", "reload_approvals", "reload_questions");
  const ended = endRunText(next, run.run_id, run.status === "cancelled");
  return { ...ended, failure: failureText(run) };
}

/** Applies one event of the stream. Returns the new state and what the caller must reload. */
export function applyStreamEvent(
  state: LiveState,
  name: string,
  data: unknown,
): { state: LiveState; effects: Effect[] } {
  const effects: Effect[] = [];
  if (data === null || typeof data !== "object") return { state, effects };
  if (name === "runs") {
    // The first event of each stream. A run that this view shows as open but that is not open
    // here ended while the view was not connected.
    const known = new Map((data as RunsEvent).runs.map((run) => [run.run_id, run]));
    let next = state;
    for (const runId of openRunIds(state)) {
      if (!known.has(runId)) {
        next = applyRun(
          next,
          { run_id: runId, status: "ended", failure_code: null, failure_message: null },
          effects,
        );
      }
    }
    for (const run of known.values()) next = applyRun(next, run, effects);
    return { state: next, effects };
  }
  if (name === "run") return { state: applyRun(state, data as StreamRun, effects), effects };
  if (name !== "run_event") return { state, effects };
  const event = data as RunEventPayload;
  const payload = event.payload ?? {};
  switch (event.type) {
    case "text_delta": {
      // A run that the view knows as ended has its reply in the messages. Replayed deltas
      // (after a reconnect) must not bring its live text back.
      const known = state.runs[event.run_id];
      if (known !== undefined && !OPEN_RUN_STATUSES.has(known.status)) return { state, effects };
      const text = typeof payload.text === "string" ? payload.text : "";
      return {
        state: { ...state, text: { ...state.text, [event.run_id]: (state.text[event.run_id] ?? "") + text } },
        effects,
      };
    }
    case "tool_call_started": {
      const call: ToolCall = {
        callId: String(payload.call_id ?? `${event.run_id}:${event.seq}`),
        name: String(payload.name ?? "tool"),
        arguments: (payload.arguments as Record<string, unknown> | undefined) ?? {},
        status: "running",
        preview: "",
      };
      if (/clarify/i.test(call.name)) effects.push("reload_questions");
      const tools = [...(state.tools[event.run_id] ?? []), call];
      return { state: { ...state, tools: { ...state.tools, [event.run_id]: tools } }, effects };
    }
    case "tool_call_finished": {
      const callId = String(payload.call_id ?? "");
      const tools = (state.tools[event.run_id] ?? []).map((call) =>
        call.callId === callId
          ? {
              ...call,
              status: payload.is_error === true ? ("error" as const) : ("done" as const),
              preview: typeof payload.output_preview === "string" ? payload.output_preview : "",
            }
          : call,
      );
      if (tools.some((call) => call.callId === callId && /clarify/i.test(call.name))) {
        effects.push("reload_questions");
      }
      return { state: { ...state, tools: { ...state.tools, [event.run_id]: tools } }, effects };
    }
    case "subagent_event": {
      // The progress of a coding agent (M7.2). Native sub-agents show nothing here.
      const line = codingProgressLine(payload);
      const known = state.runs[event.run_id];
      if (line === null || (known !== undefined && !OPEN_RUN_STATUSES.has(known.status))) {
        return { state, effects };
      }
      const before = state.text[event.run_id] ?? "";
      const gap = before === "" || before.endsWith("\n\n") ? "" : before.endsWith("\n") ? "\n" : "\n\n";
      return {
        state: { ...state, text: { ...state.text, [event.run_id]: `${before}${gap}${line}\n\n` } },
        effects,
      };
    }
    case "approval_required":
      effects.push("reload_approvals");
      return { state, effects };
    case "run_completed":
    case "run_failed":
      effects.push("reload_messages");
      return {
        state: endRunText(state, event.run_id, payload.code === "cancelled"),
        effects,
      };
    default:
      return { state, effects };
  }
}

/** `/compact` or `/compact <focus>`: the focus text, or `null` if the text is not the command. */
export function parseCompact(text: string): string | null {
  const match = /^\/compact(?:\s+([\s\S]*))?$/.exec(text.trim());
  return match ? (match[1] ?? "").trim() : null;
}

/** The title of a session in the list. */
export function sessionTitle(session: { id: string; title: string | null }): string {
  return session.title?.trim() || `Session ${session.id.slice(0, 8)}`;
}

/** The places that `/handoff` can move a session to. */
export const HANDOFF_CHANNELS = ["telegram", "slack", "web"] as const;
export type HandoffChannel = (typeof HANDOFF_CHANNELS)[number];

/** The coding agents that a session can send its messages to (M7.2). */
export const CODING_AGENTS = { claude_code: "Claude Code", codex: "Codex" } as const;
export type CodingAgent = keyof typeof CODING_AGENTS;

/** The progress line of a coding agent event, or `null` for any other sub-agent event. */
export function codingProgressLine(payload: Record<string, unknown>): string | null {
  const agent = payload.subagent;
  if (typeof agent !== "string" || !Object.hasOwn(CODING_AGENTS, agent)) return null;
  const text = typeof payload.text === "string" ? payload.text.split(/\s+/).filter(Boolean).join(" ") : "";
  return text ? `> ${CODING_AGENTS[agent as CodingAgent]}: ${text}` : null;
}

/** A busy control or a context command of the composer (M3.9). */
export type ChatCommand =
  | { kind: "compact"; focus: string }
  | { kind: "steer"; note: string }
  | { kind: "handoff"; to: HandoffChannel | null }
  | { kind: "coding"; agent: CodingAgent | null }
  | { kind: "undo" }
  | { kind: "rollback"; number: number | null; valid: boolean }
  | { kind: "bg"; task: string }
  | { kind: "stop"; number: number | null; valid: boolean };

/**
 * The command in a composer text, or `null` for a normal message. `/handoff` with an unknown
 * place gives `to: null`, so that the composer can show the usage.
 */
export function parseCommand(text: string): ChatCommand | null {
  const trimmed = text.trim();
  const focus = parseCompact(trimmed);
  if (focus !== null) return { kind: "compact", focus };
  const steer = /^\/steer(?:\s+([\s\S]*))?$/i.exec(trimmed);
  if (steer) return { kind: "steer", note: (steer[1] ?? "").trim() };
  const handoff = /^\/handoff(?:\s+(\S*))?\s*$/i.exec(trimmed);
  if (handoff) {
    const to = (handoff[1] ?? "").toLowerCase();
    return {
      kind: "handoff",
      to: (HANDOFF_CHANNELS as readonly string[]).includes(to) ? (to as HandoffChannel) : null,
    };
  }
  // `/claude TASK` is a normal message: the API reads the command. Alone, it sets the direct mode.
  const coding = /^\/(claude|codex|sama)$/i.exec(trimmed);
  if (coding) {
    const name = (coding[1] ?? "").toLowerCase();
    return { kind: "coding", agent: name === "sama" ? null : name === "claude" ? "claude_code" : "codex" };
  }
  if (/^\/undo$/i.test(trimmed)) return { kind: "undo" };
  const rollback = /^\/rollback(?:\s+(\S*))?\s*$/i.exec(trimmed);
  if (rollback) {
    // `/rollback` without a number lists the checkpoints (M6.4).
    const value = rollback[1] ?? "";
    if (!value) return { kind: "rollback", number: null, valid: true };
    const valid = /^[1-9]\d{0,5}$/.test(value);
    return { kind: "rollback", number: valid ? Number(value) : null, valid };
  }
  const bg = /^\/bg(?:\s+([\s\S]*))?$/i.exec(trimmed);
  if (bg) return { kind: "bg", task: (bg[1] ?? "").trim() };
  const stop = /^\/stop(?:\s+(all|\d+))?\s*$/i.exec(trimmed);
  if (stop) {
    // `/stop` stops all open runs, `/stop NUMBER` stops one (M7.1).
    const value = stop[1] ?? "";
    if (!value || value.toLowerCase() === "all") return { kind: "stop", number: null, valid: true };
    const valid = /^[1-9]\d{0,2}$/.test(value);
    return { kind: "stop", number: valid ? Number(value) : null, valid };
  }
  return null;
}

/** The time of a checkpoint in UTC, for example `2026-11-21 09:30 UTC`. */
export function checkpointStamp(createdAt: string): string {
  return `${new Date(createdAt).toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

/** The text of `/rollback` without a number: the checkpoints, newest first. */
export function checkpointListText(items: CheckpointItem[]): string {
  if (items.length === 0) return NO_CHECKPOINTS;
  const lines = items.map((item) => {
    const what = item.kind === "rollback" ? "the files before a restore" : `before ${item.tool}`;
    return `${item.number}. ${checkpointStamp(item.created_at)} - ${what}`;
  });
  return [
    "Checkpoints of the session files, newest first:",
    ...lines,
    "Send /rollback NUMBER to restore one. /undo restores the files before the last run.",
  ].join("\n");
}

export const NO_CHECKPOINTS = "This session has no checkpoints yet.";
export const ROLLBACK_USAGE = "Write the number of a checkpoint, for example: /rollback 2";

/** The text that the chat shows when a message waits behind an open run. */
export const QUEUED_NOTICE =
  "Queued. sama answers after the current run. Type /steer TEXT to add a note to the current run, or press Stop.";

/** The text that the chat shows when a message joins the running run (busy mode `steer`, M7.1). */
export const JOINED_NOTICE =
  "sama adds the message to the current run. The run reads it at its next step. If the run ends first, the message starts the next run.";

/** The usage text of `/bg` and of a wrong `/stop NUMBER`. */
export const BG_USAGE = "Write the task after the command, for example: /bg summarise the logs";
export const STOP_USAGE = "Write the number of a run, for example: /stop 2. /stop with no number stops all runs.";

/** The label of a note that joined a run, by its status. `null`: do not show the note. */
export function noteLabel(note: Pick<SteerNote, "status">): string | null {
  switch (note.status) {
    case "pending":
      return "Queued into the current run";
    case "delivered":
      return "Delivered to the run";
    case "dropped":
      return "Not delivered: the run stopped";
    default:
      // A requeued note is a message of the session now: the message shows it.
      return null;
  }
}

/** True while a note waits for its run: the chat looks again until the run reads it. */
export function hasPendingNote(notes: readonly Pick<SteerNote, "status">[]): boolean {
  return notes.some((note) => note.status === "pending");
}

/** The text that the chat shows when a note goes to the open run. */
export const STEERED_NOTICE = "sama adds the note to the current run. The run reads it at its next step.";

/** The usage text of `/steer` and `/handoff`. */
export const STEER_USAGE = "Write the note after the command, for example: /steer use metric units";
export const HANDOFF_USAGE = "Write where the session goes: /handoff telegram or /handoff slack";
