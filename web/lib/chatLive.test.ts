import { describe, expect, it } from "vitest";
import { applyActivity, applyStreamEvent, initialLive, type LiveState, phaseLabel } from "./chat";

const ev = (seq: number, type: string, payload: Record<string, unknown>, runId = "r1") => ({
  run_id: runId,
  seq,
  type,
  payload,
});

function apply(state: LiveState, ...events: ReturnType<typeof ev>[]): LiveState {
  return events.reduce((s, e) => applyStreamEvent(s, "run_event", e).state, state);
}

const open = applyStreamEvent(initialLive, "runs", {
  runs: [{ run_id: "r1", status: "running", failure_code: null, failure_message: null }],
}).state;

describe("live segments", () => {
  it("keeps text and tool calls in the order of the events", () => {
    const state = apply(
      open,
      ev(1, "text_delta", { text: "Let me " }),
      ev(2, "text_delta", { text: "look." }),
      ev(3, "tool_call_started", { call_id: "c1", name: "Bash", arguments: { command: "ls" } }),
      ev(4, "tool_call_finished", { call_id: "c1", output_preview: "a.txt" }),
      ev(5, "text_delta", { text: "Done." }),
    );
    expect(state.segments.r1).toEqual([
      { kind: "text", seq: 1, text: "Let me look." },
      { kind: "tool", seq: 3, callId: "c1" },
      { kind: "text", seq: 5, text: "Done." },
    ]);
    expect(state.tools.r1?.[0]).toMatchObject({ status: "done", preview: "a.txt" });
    expect(state.text.r1).toBe("Let me look.Done.");
  });

  it("drops the segments and the phase when the run ends", () => {
    let state = apply(open, ev(1, "run_phase", { phase: "thinking" }), ev(2, "text_delta", { text: "Hi" }));
    expect(state.phase.r1).toBe("thinking");
    state = apply(state, ev(3, "run_completed", {}));
    expect(state.segments).toEqual({});
    expect(state.phase).toEqual({});
  });

  it("ignores a phase of a run that ended", () => {
    const ended = applyStreamEvent(initialLive, "runs", {
      runs: [{ run_id: "r1", status: "completed", failure_code: null, failure_message: null }],
    }).state;
    expect(apply(ended, ev(1, "run_phase", { phase: "thinking" })).phase).toEqual({});
  });

  it("does not add a call twice", () => {
    const start = ev(3, "tool_call_started", { call_id: "c1", name: "Read", arguments: {} });
    const state = apply(open, start, start);
    expect(state.tools.r1).toHaveLength(1);
    expect(state.segments.r1).toHaveLength(1);
  });
});

describe("applyActivity", () => {
  it("adds the trail of runs that ended, in seq order, without live segments", () => {
    const state = applyActivity(initialLive, [
      ev(4, "tool_call_started", { call_id: "b", name: "Write", arguments: {} }, "old"),
      ev(2, "tool_call_started", { call_id: "a", name: "Read", arguments: {} }, "old"),
      ev(5, "tool_call_finished", { call_id: "b", is_error: true, output_preview: "denied" }, "old"),
      ev(6, "text_delta", { text: "ignored" }, "old"),
    ]);
    expect(state.tools.old?.map((c) => [c.callId, c.status])).toEqual([
      ["a", "running"],
      ["b", "error"],
    ]);
    expect(state.segments).toEqual({});
    expect(state.text).toEqual({});
  });

  it("merges with calls that the stream gave", () => {
    const live = apply(open, ev(3, "tool_call_started", { call_id: "c1", name: "Bash", arguments: {} }));
    const state = applyActivity(live, [ev(3, "tool_call_started", { call_id: "c1", name: "Bash", arguments: {} })]);
    expect(state.tools.r1).toHaveLength(1);
    expect(state.segments.r1).toHaveLength(1);
  });
});

describe("phaseLabel", () => {
  it("names the phase until the run shows activity", () => {
    expect(phaseLabel("running", "starting_sandbox", false)).toBe("Starting the sandbox");
    expect(phaseLabel("running", "some_new_phase", false)).toBe("Some new phase");
    expect(phaseLabel("running", "thinking", true)).toBe("Working");
    expect(phaseLabel("queued", undefined, false)).toBe("Waiting to start");
    expect(phaseLabel("paused", "thinking", true)).toBe("Waiting for you");
    expect(phaseLabel("running", undefined, false)).toBe("Starting");
  });
});

describe("finish before start", () => {
  it("keeps a finish that comes before its start, in both orders", () => {
    const start = { ...ev(3, "tool_call_started", { call_id: "c1", name: "Bash", arguments: { command: "ls" } }), id: 30 };
    const finish = { ...ev(4, "tool_call_finished", { call_id: "c1", output_preview: "a.txt" }), id: 31 };
    const late = applyActivity(apply(open, finish), [start]);
    const normal = applyActivity(open, [start, finish]);
    for (const state of [late, normal]) {
      expect(state.tools.r1).toHaveLength(1);
      expect(state.tools.r1?.[0]).toMatchObject({
        name: "Bash",
        status: "done",
        preview: "a.txt",
        order: 30,
        arguments: { command: "ls" },
      });
      expect(state.tools.r1?.[0]?.placeholder).toBeFalsy();
    }
  });
});
