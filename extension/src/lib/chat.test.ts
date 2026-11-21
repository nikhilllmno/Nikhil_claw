import { describe, expect, it } from "vitest";

import { applyEvent, initialLive, isPaused, openRunIds } from "./chat";

const run = (status: string, failure_message: string | null = null) => ({
  run_id: "r1",
  status,
  failure_code: null,
  failure_message,
});

describe("applyEvent", () => {
  it("streams the text of an open run and reloads the messages at the end", () => {
    let state = applyEvent(initialLive, "runs", { runs: [run("running")] }).state;
    state = applyEvent(state, "run_event", { run_id: "r1", seq: 1, type: "text_delta", payload: { text: "hel" } }).state;
    state = applyEvent(state, "run_event", { run_id: "r1", seq: 2, type: "text_delta", payload: { text: "lo" } }).state;
    expect(state.text.r1).toBe("hello");
    const ended = applyEvent(state, "run", run("completed"));
    expect(ended.effects).toContain("reload_messages");
    expect(ended.state.text.r1).toBeUndefined();
    expect(openRunIds(ended.state)).toEqual([]);
  });

  it("shows the failure of a run", () => {
    const state = applyEvent(initialLive, "run", run("running")).state;
    expect(applyEvent(state, "run", run("failed", "boom")).state.failure).toBe("The run failed: boom");
  });

  it("asks for the approvals of a paused run", () => {
    const result = applyEvent(initialLive, "run", run("paused"));
    expect(result.effects).toEqual(["reload_approvals"]);
    expect(isPaused(result.state)).toBe(true);
  });

  it("tracks tool calls", () => {
    let state = applyEvent(initialLive, "run", run("running")).state;
    state = applyEvent(state, "run_event", {
      run_id: "r1",
      seq: 1,
      type: "tool_call_started",
      payload: { call_id: "c1", name: "web_search" },
    }).state;
    state = applyEvent(state, "run_event", {
      run_id: "r1",
      seq: 2,
      type: "tool_call_finished",
      payload: { call_id: "c1", is_error: true },
    }).state;
    expect(state.tools.r1).toEqual([{ callId: "c1", name: "web_search", status: "error" }]);
  });

  it("ends a run that the first runs event does not have", () => {
    const state = applyEvent(initialLive, "run", run("running")).state;
    const result = applyEvent(state, "runs", { runs: [] });
    expect(openRunIds(result.state)).toEqual([]);
    expect(result.effects).toContain("reload_messages");
  });

  it("ignores late deltas of an ended run", () => {
    const state = applyEvent(initialLive, "run", run("completed")).state;
    const next = applyEvent(state, "run_event", { run_id: "r1", type: "text_delta", payload: { text: "x" } });
    expect(next.state.text.r1).toBeUndefined();
  });
});
