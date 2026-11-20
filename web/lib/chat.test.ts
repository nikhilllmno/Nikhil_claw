import { describe, expect, it } from "vitest";
import { applyStreamEvent, initialLive, isPaused, openRunIds, parseCompact, sessionTitle } from "./chat";

const run = (id: string, status: string, message: string | null = null) => ({
  run_id: id,
  status,
  failure_code: null,
  failure_message: message,
});

describe("applyStreamEvent", () => {
  it("adds streamed text per run", () => {
    let state = initialLive;
    for (const text of ["Hel", "lo"]) {
      state = applyStreamEvent(state, "run_event", {
        run_id: "r1",
        seq: 1,
        type: "text_delta",
        payload: { text },
      }).state;
    }
    expect(state.text).toEqual({ r1: "Hello" });
  });

  it("tracks tool calls from start to finish", () => {
    let state = initialLive;
    state = applyStreamEvent(state, "run_event", {
      run_id: "r1",
      seq: 1,
      type: "tool_call_started",
      payload: { call_id: "c1", name: "Bash", arguments: { command: "ls" } },
    }).state;
    expect(state.tools.r1?.[0]).toMatchObject({ name: "Bash", status: "running" });
    state = applyStreamEvent(state, "run_event", {
      run_id: "r1",
      seq: 2,
      type: "tool_call_finished",
      payload: { call_id: "c1", is_error: true, output_preview: "boom" },
    }).state;
    expect(state.tools.r1?.[0]).toMatchObject({ status: "error", preview: "boom" });
  });

  it("asks for a reload of the questions at a clarify call", () => {
    const { effects } = applyStreamEvent(initialLive, "run_event", {
      run_id: "r1",
      seq: 1,
      type: "tool_call_started",
      payload: { call_id: "c1", name: "mcp__sama__clarify" },
    });
    expect(effects).toEqual(["reload_questions"]);
  });

  it("asks for approvals when a run pauses or needs one", () => {
    expect(
      applyStreamEvent(initialLive, "run_event", {
        run_id: "r1",
        seq: 3,
        type: "approval_required",
        payload: {},
      }).effects,
    ).toEqual(["reload_approvals"]);
    const { state, effects } = applyStreamEvent(initialLive, "run", run("r1", "paused"));
    expect(effects).toEqual(["reload_approvals"]);
    expect(isPaused(state)).toBe(true);
  });

  it("reloads messages and clears the live text when an open run ends", () => {
    let state = applyStreamEvent(initialLive, "runs", { runs: [run("r1", "running")] }).state;
    state = applyStreamEvent(state, "run_event", {
      run_id: "r1",
      seq: 1,
      type: "text_delta",
      payload: { text: "x" },
    }).state;
    expect(openRunIds(state)).toEqual(["r1"]);
    const ended = applyStreamEvent(state, "run", run("r1", "completed"));
    expect(ended.effects).toContain("reload_messages");
    expect(ended.state.text).toEqual({});
    expect(openRunIds(ended.state)).toEqual([]);
    expect(ended.state.failure).toBeNull();
  });

  it("shows the failure of a run that ended in this view", () => {
    const state = applyStreamEvent(initialLive, "run", run("r1", "running")).state;
    const ended = applyStreamEvent(state, "run", run("r1", "failed", "the model failed"));
    expect(ended.state.failure).toBe("The run failed: the model failed");
  });

  it("ignores the end of a run that was never shown as open", () => {
    const { state, effects } = applyStreamEvent(initialLive, "runs", {
      runs: [run("old", "failed", "old news")],
    });
    expect(effects).toEqual([]);
    expect(state.failure).toBeNull();
  });

  it("closes a run that ended while the stream was away", () => {
    const open = applyStreamEvent(initialLive, "run", run("r1", "running")).state;
    const { state, effects } = applyStreamEvent(open, "runs", { runs: [] });
    expect(openRunIds(state)).toEqual([]);
    expect(effects).toContain("reload_messages");
  });

  it("ignores unknown events and empty data", () => {
    expect(applyStreamEvent(initialLive, "other", {}).state).toBe(initialLive);
    expect(applyStreamEvent(initialLive, "run", null).state).toBe(initialLive);
  });
});

describe("parseCompact", () => {
  it("reads the command and its focus", () => {
    expect(parseCompact("/compact")).toBe("");
    expect(parseCompact("  /compact keep the plan ")).toBe("keep the plan");
    expect(parseCompact("/compactor")).toBeNull();
    expect(parseCompact("please /compact")).toBeNull();
  });
});

describe("sessionTitle", () => {
  it("falls back to a short id", () => {
    expect(sessionTitle({ id: "12345678-aaaa", title: null })).toBe("Session 12345678");
    expect(sessionTitle({ id: "x", title: "  Plan " })).toBe("Plan");
  });
});
