// The live state of the chat of one tab, built from the session events of the socket. The
// same rules as web/lib/chat.ts. The functions are pure, so tests can run them.

export interface StreamRun {
  run_id: string;
  status: string;
  failure_code: string | null;
  failure_message: string | null;
}

export interface ToolCall {
  callId: string;
  name: string;
  status: "running" | "done" | "error";
}

export interface LiveState {
  runs: Record<string, StreamRun>;
  /** The streamed text of each open run. */
  text: Record<string, string>;
  tools: Record<string, ToolCall[]>;
  failure: string | null;
}

export type Effect = "reload_messages" | "reload_approvals";

export const OPEN_RUN_STATUSES: ReadonlySet<string> = new Set(["queued", "running", "paused"]);

export const initialLive: LiveState = { runs: {}, text: {}, tools: {}, failure: null };

export function openRunIds(state: LiveState): string[] {
  return Object.values(state.runs)
    .filter((run) => OPEN_RUN_STATUSES.has(run.status))
    .map((run) => run.run_id);
}

export function isPaused(state: LiveState): boolean {
  return Object.values(state.runs).some((run) => run.status === "paused");
}

function withoutRun<T>(record: Record<string, T>, runId: string): Record<string, T> {
  return Object.fromEntries(Object.entries(record).filter(([id]) => id !== runId));
}

function applyRun(state: LiveState, run: StreamRun, effects: Effect[]): LiveState {
  const before = state.runs[run.run_id];
  const wasOpen = before !== undefined && OPEN_RUN_STATUSES.has(before.status);
  const next: LiveState = { ...state, runs: { ...state.runs, [run.run_id]: run } };
  if (run.status === "paused") {
    effects.push("reload_approvals");
    return next;
  }
  if (OPEN_RUN_STATUSES.has(run.status)) return { ...next, failure: null };
  if (!wasOpen) return next;
  effects.push("reload_messages", "reload_approvals");
  const failure =
    run.status === "failed"
      ? run.failure_message
        ? `The run failed: ${run.failure_message}`
        : "The run failed."
      : null;
  return {
    ...next,
    text: withoutRun(next.text, run.run_id),
    tools: withoutRun(next.tools, run.run_id),
    failure,
  };
}

/** Applies one session event. Returns the new state and what the caller must reload. */
export function applyEvent(
  state: LiveState,
  type: string,
  data: Record<string, unknown>,
): { state: LiveState; effects: Effect[] } {
  const effects: Effect[] = [];
  if (type === "runs") {
    const runs = Array.isArray(data.runs) ? (data.runs as StreamRun[]) : [];
    const known = new Map(runs.map((run) => [run.run_id, run]));
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
  if (type === "run") return { state: applyRun(state, data as unknown as StreamRun, effects), effects };
  if (type !== "run_event") return { state, effects };
  const runId = String(data.run_id ?? "");
  const payload = (data.payload as Record<string, unknown> | undefined) ?? {};
  switch (data.type) {
    case "text_delta": {
      const known = state.runs[runId];
      if (known !== undefined && !OPEN_RUN_STATUSES.has(known.status)) return { state, effects };
      const text = typeof payload.text === "string" ? payload.text : "";
      return {
        state: { ...state, text: { ...state.text, [runId]: (state.text[runId] ?? "") + text } },
        effects,
      };
    }
    case "tool_call_started": {
      const call: ToolCall = {
        callId: String(payload.call_id ?? `${runId}:${String(data.seq)}`),
        name: String(payload.name ?? "tool"),
        status: "running",
      };
      const tools = [...(state.tools[runId] ?? []), call];
      return { state: { ...state, tools: { ...state.tools, [runId]: tools } }, effects };
    }
    case "tool_call_finished": {
      const callId = String(payload.call_id ?? "");
      const tools = (state.tools[runId] ?? []).map((call) =>
        call.callId === callId
          ? { ...call, status: payload.is_error === true ? ("error" as const) : ("done" as const) }
          : call,
      );
      return { state: { ...state, tools: { ...state.tools, [runId]: tools } }, effects };
    }
    case "approval_required":
      effects.push("reload_approvals");
      return { state, effects };
    case "run_completed":
    case "run_failed":
      effects.push("reload_messages");
      return { state: { ...state, text: withoutRun(state.text, runId) }, effects };
    default:
      return { state, effects };
  }
}
