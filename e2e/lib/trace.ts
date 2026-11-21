// The tool calls of a session, from the run traces (GET /runs/{id}/trace). A test uses them to
// check that the agent really called a tool, not only that the answer has the right words.

import type { SamaApi } from "./api.ts";

export interface ToolUse {
  name: string;
  arguments: unknown;
  /** True when the call finished without an error. */
  ok: boolean;
  output: string;
}

interface TraceStep {
  type: string;
  payload: {
    name?: string;
    call_id?: string;
    arguments?: unknown;
    is_error?: boolean;
    output_preview?: string | null;
  };
}

/** Pairs the start and the end events of each tool call of a trace. */
export function toolUsesOf(steps: TraceStep[]): ToolUse[] {
  const started = new Map<string, ToolUse>();
  const order: ToolUse[] = [];
  for (const step of steps) {
    const id = step.payload.call_id;
    if (!id) continue;
    if (step.type === "tool_call_started") {
      const use = { name: step.payload.name ?? "", arguments: step.payload.arguments, ok: false, output: "" };
      started.set(id, use);
      order.push(use);
    } else if (step.type === "tool_call_finished") {
      const use = started.get(id);
      if (use) {
        use.ok = step.payload.is_error !== true;
        use.output = step.payload.output_preview ?? "";
      }
    }
  }
  return order;
}

/** The tool calls of all runs of a session, oldest run first. */
export async function sessionToolUses(api: SamaApi, sessionId: string): Promise<ToolUse[]> {
  const runs = await api.runs(sessionId);
  const uses: ToolUse[] = [];
  for (const run of [...runs].reverse()) {
    const trace = await api.get<{ steps: TraceStep[] }>(`/runs/${run.id}/trace`);
    uses.push(...toolUsesOf(trace.steps));
  }
  return uses;
}

/**
 * True when a call used the tool: directly, or through `tool_call` for a deferred tool
 * (architecture Section 6.5), where the arguments name the tool.
 */
export function calledTool(use: ToolUse, match: RegExp): boolean {
  if (match.test(use.name)) return true;
  if (!use.name.endsWith("tool_call")) return false;
  const args = use.arguments as { name?: unknown } | null | undefined;
  return typeof args?.name === "string" && match.test(args.name);
}
