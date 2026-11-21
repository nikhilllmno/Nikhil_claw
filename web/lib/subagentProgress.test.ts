import { describe, expect, it } from "vitest";
import type { TraceStep } from "./api";
import { applyStreamEvent, initialLive } from "./chat";
import {
  addSubagentEvent,
  groupSubagents,
  parseSubagentEvent,
  placeSubagents,
  runHasEnded,
  type SubagentEvent,
  subagentStepsLabel,
  traceSubagentEvents,
  subagentName,
} from "./subagentProgress";

const ev = (seq: number, over: Partial<SubagentEvent> = {}): SubagentEvent => ({
  seq,
  subagent: "researcher",
  text: `step ${seq}`,
  callId: "c1",
  done: false,
  tokens: null,
  ...over,
});

describe("parseSubagentEvent", () => {
  it("reads a valid payload", () => {
    expect(
      parseSubagentEvent(4, { subagent: " web ", text: " Open page ", call_id: "c9", done: true, tokens: 120 }),
    ).toEqual({ seq: 4, subagent: "web", text: "Open page", callId: "c9", done: true, tokens: 120 });
  });

  it("uses safe defaults", () => {
    expect(parseSubagentEvent(1, { subagent: "web", call_id: null, tokens: "9", done: "yes" })).toEqual({
      seq: 1,
      subagent: "web",
      text: "",
      callId: null,
      done: false,
      tokens: null,
    });
    expect(parseSubagentEvent(1, { subagent: "web", call_id: "", tokens: -1 })).toMatchObject({
      callId: null,
      tokens: null,
    });
  });

  it("rejects an event without a name", () => {
    expect(parseSubagentEvent(1, { text: "x" })).toBeNull();
    expect(parseSubagentEvent(1, { subagent: "  " })).toBeNull();
    expect(parseSubagentEvent(1, { subagent: 5 })).toBeNull();
  });

  it("keeps text as plain text", () => {
    expect(parseSubagentEvent(1, { subagent: "a", text: "<b>**x**</b>" })?.text).toBe("<b>**x**</b>");
  });
});

describe("addSubagentEvent", () => {
  it("keeps the order and ignores a repeated seq", () => {
    let list: SubagentEvent[] = [];
    list = addSubagentEvent(list, ev(3));
    list = addSubagentEvent(list, ev(1));
    const same = addSubagentEvent(list, ev(3, { text: "other" }));
    expect(same).toBe(list);
    expect(list.map((e) => e.seq)).toEqual([1, 3]);
  });
});

describe("groupSubagents", () => {
  it("groups by call_id, in order", () => {
    const groups = groupSubagents([
      ev(1, { callId: "a" }),
      ev(2, { callId: "b" }),
      ev(3, { callId: "a" }),
      ev(4, { callId: "b", done: true, tokens: 50 }),
    ]);
    expect(groups.map((g) => [g.callId, g.steps.map((s) => s.seq), g.done, g.tokens])).toEqual([
      ["a", [1, 3], false, null],
      ["b", [2, 4], true, 50],
    ]);
    expect(groups[0]?.firstSeq).toBe(1);
  });

  it("sorts events that arrive out of order", () => {
    const [group] = groupSubagents([ev(3), ev(1), ev(2)]);
    expect(group?.steps.map((s) => s.text)).toEqual(["step 1", "step 2", "step 3"]);
  });

  it("groups events without call_id by name", () => {
    const groups = groupSubagents([
      ev(1, { callId: null, subagent: "x" }),
      ev(2, { callId: null, subagent: "y" }),
      ev(3, { callId: null, subagent: "x" }),
    ]);
    expect(groups.map((g) => [g.subagent, g.steps.length])).toEqual([
      ["x", 2],
      ["y", 1],
    ]);
  });

  it("keeps a name group and a call group of the same name apart", () => {
    const groups = groupSubagents([ev(1, { callId: null }), ev(2, { callId: "c1" })]);
    expect(groups).toHaveLength(2);
  });

  it("opens a new name group after a done event", () => {
    const groups = groupSubagents([
      ev(1, { callId: null }),
      ev(2, { callId: null, done: true }),
      ev(3, { callId: null }),
    ]);
    expect(groups.map((g) => [g.steps.length, g.done])).toEqual([
      [2, true],
      [1, false],
    ]);
  });

  it("is done when an event has done, or when the run ended", () => {
    expect(groupSubagents([ev(1)])[0]?.done).toBe(false);
    expect(groupSubagents([ev(1), ev(2, { done: true })])[0]?.done).toBe(true);
    expect(groupSubagents([ev(1)], true)[0]?.done).toBe(true);
  });

  it("adds no step for an event without text, but keeps its state and tokens", () => {
    const [group] = groupSubagents([ev(1), ev(2, { text: "", done: true, tokens: 7 })]);
    expect(group).toMatchObject({ done: true, tokens: 7 });
    expect(group?.steps).toHaveLength(1);
  });

  it("returns no group for no event", () => {
    expect(groupSubagents([])).toEqual([]);
  });
});

describe("placeSubagents", () => {
  it("nests a group under the call with its id and leaves the others as own rows", () => {
    const groups = groupSubagents([
      ev(2, { callId: "c1" }),
      ev(3, { callId: "gone" }),
      ev(4, { callId: null, subagent: "z" }),
    ]);
    const { nested, own } = placeSubagents(groups, ["c0", "c1"]);
    expect([...nested.keys()]).toEqual(["c1"]);
    expect(nested.get("c1")?.[0]?.firstSeq).toBe(2);
    expect(own.map((g) => g.firstSeq)).toEqual([3, 4]);
  });
});

describe("runHasEnded", () => {
  it("is true only for a known status that is not open", () => {
    expect(runHasEnded(undefined)).toBe(false);
    expect(runHasEnded("running")).toBe(false);
    expect(runHasEnded("paused")).toBe(false);
    expect(runHasEnded("completed")).toBe(true);
    expect(runHasEnded("failed")).toBe(true);
  });
});

describe("traceSubagentEvents (history rebuild)", () => {
  it("rebuilds the same groups from stored steps", () => {
    const step = (seq: number, type: string, payload: Record<string, unknown>): TraceStep => ({
      seq,
      type,
      created_at: "2026-01-01T00:00:00Z",
      payload,
    });
    const events = traceSubagentEvents([
      step(1, "tool_call_started", { name: "Task" }),
      step(2, "subagent_event", { subagent: "r", text: "a", call_id: "c1" }),
      step(3, "subagent_event", { text: "no name" }),
      step(4, "subagent_event", { subagent: "r", text: "b", call_id: "c1", done: true, tokens: 9 }),
    ]);
    const [group] = groupSubagents(events);
    expect(group).toMatchObject({ callId: "c1", done: true, tokens: 9 });
    expect(group?.steps.map((s) => s.text)).toEqual(["a", "b"]);
  });
});

describe("subagentStepsLabel", () => {
  it("writes the count", () => {
    expect(subagentStepsLabel(0)).toBe("0 steps");
    expect(subagentStepsLabel(1)).toBe("1 step");
    expect(subagentStepsLabel(2)).toBe("2 steps");
  });
});

describe("applyStreamEvent with subagent_event", () => {
  const send = (state: typeof initialLive, seq: number, payload: Record<string, unknown>, runId = "r1") =>
    applyStreamEvent(state, "run_event", { run_id: runId, seq, type: "subagent_event", payload });

  it("collects the events of each run and ignores a repeated one", () => {
    let state = initialLive;
    state = send(state, 1, { subagent: "w", text: "a", call_id: "c1" }).state;
    state = send(state, 2, { subagent: "w", text: "b", call_id: "c1" }, "r2").state;
    const again = send(state, 1, { subagent: "w", text: "a", call_id: "c1" });
    expect(again.state).toBe(state);
    expect(again.effects).toEqual([]);
    expect(Object.keys(state.subagents)).toEqual(["r1", "r2"]);
    expect(state.subagents.r1?.[0]).toMatchObject({ subagent: "w", text: "a", callId: "c1" });
  });

  it("ignores an event without a name and changes no tool", () => {
    const state = send(initialLive, 1, { text: "x" }).state;
    expect(state).toBe(initialLive);
  });

  it("nests the group under the tool call of the same run", () => {
    let state = applyStreamEvent(initialLive, "run_event", {
      run_id: "r1",
      seq: 1,
      type: "tool_call_started",
      payload: { call_id: "c1", name: "Task" },
    }).state;
    state = send(state, 2, { subagent: "w", text: "a", call_id: "c1" }).state;
    state = send(state, 3, { subagent: "w", text: "b", call_id: "c1", done: true, tokens: 5 }).state;
    const calls = state.tools.r1 ?? [];
    const { nested, own } = placeSubagents(
      groupSubagents(state.subagents.r1 ?? []),
      calls.map((c) => c.callId),
    );
    expect(own).toEqual([]);
    expect(nested.get("c1")?.[0]).toMatchObject({ done: true, tokens: 5 });
    expect(calls[0]?.seq).toBe(1);
  });
});

describe("subagentName", () => {
  it("names the coding agents for people", () => {
    expect(subagentName("claude_code")).toBe("Claude Code");
    expect(subagentName("codex")).toBe("Codex");
    expect(subagentName("reviewer")).toBe("reviewer");
    expect(subagentName("toString")).toBe("toString");
  });
});
