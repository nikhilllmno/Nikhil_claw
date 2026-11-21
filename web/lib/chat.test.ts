import { describe, expect, it } from "vitest";
import {
  applyStreamEvent,
  checkpointListText,
  codingProgressLine,
  hasPendingNote,
  initialLive,
  isPaused,
  NO_CHECKPOINTS,
  noteLabel,
  openRunIds,
  parseCommand,
  parseCompact,
  sessionTitle,
} from "./chat";

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

  it("does not bring back the text of a run that the snapshot shows as completed", () => {
    const delta = (text: string, seq: number) => ({
      run_id: "r1",
      seq,
      type: "text_delta",
      payload: { text },
    });
    let state = applyStreamEvent(initialLive, "runs", { runs: [run("r1", "completed")] }).state;
    for (const [i, text] of ["Hel", "lo"].entries()) {
      state = applyStreamEvent(state, "run_event", delta(text, i + 1)).state;
    }
    expect(state.text).toEqual({});
    const done = applyStreamEvent(state, "run_event", {
      run_id: "r1",
      seq: 3,
      type: "run_completed",
      payload: {},
    });
    expect(done.effects).toEqual(["reload_messages"]);
    expect(done.state.text).toEqual({});
  });

  it("shows the progress of a coding agent as lines of the live text", () => {
    const event = (seq: number, type: string, payload: Record<string, unknown>) => ({
      run_id: "r1",
      seq,
      type,
      payload,
    });
    let state = applyStreamEvent(initialLive, "run_event", event(1, "text_delta", { text: "Start" })).state;
    state = applyStreamEvent(
      state,
      "run_event",
      event(2, "subagent_event", { subagent: "claude_code", text: "edited  main.py\nand tests" }),
    ).state;
    state = applyStreamEvent(
      state,
      "run_event",
      event(3, "subagent_event", { subagent: "codex", text: "ran pytest" }),
    ).state;
    expect(state.text.r1).toBe("Start\n\n> Claude Code: edited main.py and tests\n\n> Codex: ran pytest\n\n");
  });

  it("ignores native sub-agent events and the events of an ended run", () => {
    const native = applyStreamEvent(initialLive, "run_event", {
      run_id: "r1",
      seq: 1,
      type: "subagent_event",
      payload: { subagent: "reviewer", text: "reading" },
    });
    expect(native.state).toBe(initialLive);
    const ended = applyStreamEvent(initialLive, "runs", { runs: [run("r1", "completed")] }).state;
    const late = applyStreamEvent(ended, "run_event", {
      run_id: "r1",
      seq: 2,
      type: "subagent_event",
      payload: { subagent: "codex", text: "ran pytest" },
    });
    expect(late.state.text).toEqual({});
  });

  it("clears the live text of a run at its terminal event", () => {
    let state = applyStreamEvent(initialLive, "run_event", {
      run_id: "r1",
      seq: 1,
      type: "text_delta",
      payload: { text: "Hi" },
    }).state;
    state = applyStreamEvent(state, "run_event", {
      run_id: "r2",
      seq: 1,
      type: "text_delta",
      payload: { text: "Other" },
    }).state;
    for (const type of ["run_completed", "run_failed"]) {
      const ended = applyStreamEvent(state, "run_event", { run_id: "r1", seq: 2, type, payload: {} });
      expect(ended.state.text).toEqual({ r2: "Other" });
    }
  });

  it("keeps the partial text of a stopped run and marks it", () => {
    let state = applyStreamEvent(initialLive, "run", {
      run_id: "r1",
      status: "running",
      failure_code: null,
      failure_message: null,
    }).state;
    state = applyStreamEvent(state, "run_event", {
      run_id: "r1",
      seq: 1,
      type: "text_delta",
      payload: { text: "Once upon" },
    }).state;
    state = applyStreamEvent(state, "run_event", {
      run_id: "r1",
      seq: 2,
      type: "run_failed",
      payload: { code: "cancelled", message: "cancelled" },
    }).state;
    expect(state.text).toEqual({});
    expect(state.stopped).toEqual({ r1: "Once upon" });
    // The status event that follows does not lose the text, and shows no failure.
    state = applyStreamEvent(state, "run", {
      run_id: "r1",
      status: "cancelled",
      failure_code: "cancelled",
      failure_message: "cancelled",
    }).state;
    expect(state.stopped).toEqual({ r1: "Once upon" });
    expect(state.failure).toBeNull();
  });

  it("marks a run as stopped when only its status says so", () => {
    let state = applyStreamEvent(initialLive, "run", {
      run_id: "r1",
      status: "running",
      failure_code: null,
      failure_message: null,
    }).state;
    state = applyStreamEvent(state, "run_event", {
      run_id: "r1",
      seq: 1,
      type: "text_delta",
      payload: { text: "Part" },
    }).state;
    state = applyStreamEvent(state, "run", {
      run_id: "r1",
      status: "cancelled",
      failure_code: "cancelled",
      failure_message: "cancelled",
    }).state;
    expect(state.stopped).toEqual({ r1: "Part" });
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

describe("parseCommand", () => {
  it("reads the busy controls and the compact command", () => {
    expect(parseCommand("/compact keep it")).toEqual({ kind: "compact", focus: "keep it" });
    expect(parseCommand(" /steer use metric units ")).toEqual({ kind: "steer", note: "use metric units" });
    expect(parseCommand("/steer two\nlines")).toEqual({ kind: "steer", note: "two\nlines" });
    expect(parseCommand("/steer")).toEqual({ kind: "steer", note: "" });
    expect(parseCommand("/stop")).toEqual({ kind: "stop", number: null, valid: true });
    expect(parseCommand("/stop all")).toEqual({ kind: "stop", number: null, valid: true });
    expect(parseCommand("/stop 2")).toEqual({ kind: "stop", number: 2, valid: true });
    expect(parseCommand("/stop 0")).toEqual({ kind: "stop", number: null, valid: false });
    expect(parseCommand("/bg  check the logs ")).toEqual({ kind: "bg", task: "check the logs" });
    expect(parseCommand("/bg")).toEqual({ kind: "bg", task: "" });
    expect(parseCommand("/handoff Telegram")).toEqual({ kind: "handoff", to: "telegram" });
    expect(parseCommand("/handoff slack")).toEqual({ kind: "handoff", to: "slack" });
    expect(parseCommand("/handoff web")).toEqual({ kind: "handoff", to: "web" });
  });

  it("gives no place for an unknown or a missing handoff target", () => {
    expect(parseCommand("/handoff")).toEqual({ kind: "handoff", to: null });
    expect(parseCommand("/handoff mars")).toEqual({ kind: "handoff", to: null });
    expect(parseCommand("/handoff slack now")).toBeNull();
  });

  it("reads the coding agent commands", () => {
    expect(parseCommand("/claude")).toEqual({ kind: "coding", agent: "claude_code" });
    expect(parseCommand(" /Codex ")).toEqual({ kind: "coding", agent: "codex" });
    expect(parseCommand("/sama")).toEqual({ kind: "coding", agent: null });
    // A task is a normal message: the API reads the command.
    expect(parseCommand("/claude fix the tests")).toBeNull();
    expect(parseCommand("/codex fix the tests")).toBeNull();
  });

  it("gives the progress line of a coding agent only", () => {
    expect(codingProgressLine({ subagent: "codex", text: "ran pytest" })).toBe("> Codex: ran pytest");
    expect(codingProgressLine({ subagent: "codex", text: "  " })).toBeNull();
    expect(codingProgressLine({ subagent: "toString", text: "x" })).toBeNull();
    expect(codingProgressLine({ text: "x" })).toBeNull();
  });

  it("reads the checkpoint commands", () => {
    expect(parseCommand(" /undo ")).toEqual({ kind: "undo" });
    expect(parseCommand("/rollback")).toEqual({ kind: "rollback", number: null, valid: true });
    expect(parseCommand("/Rollback 2")).toEqual({ kind: "rollback", number: 2, valid: true });
    expect(parseCommand("/rollback two")).toEqual({ kind: "rollback", number: null, valid: false });
    expect(parseCommand("/rollback 0")).toEqual({ kind: "rollback", number: null, valid: false });
    expect(parseCommand("/rollback 2 3")).toBeNull();
    expect(parseCommand("/undo it")).toBeNull();
  });

  it("treats other text as a normal message", () => {
    expect(parseCommand("hello")).toBeNull();
    expect(parseCommand("/steering wheel")).toBeNull();
    expect(parseCommand("/stop now")).toBeNull();
    expect(parseCommand("please /steer")).toBeNull();
    expect(parseCommand("/bgcolor red")).toBeNull();
  });
});

describe("noteLabel", () => {
  it("names each state of a message that joined a run", () => {
    expect(noteLabel({ status: "pending" })).toBe("Queued into the current run");
    expect(noteLabel({ status: "delivered" })).toBe("Delivered to the run");
    expect(noteLabel({ status: "dropped" })).toBe("Not delivered: the run stopped");
    expect(noteLabel({ status: "requeued" })).toBeNull();
    expect(hasPendingNote([{ status: "delivered" }, { status: "pending" }])).toBe(true);
    expect(hasPendingNote([{ status: "delivered" }])).toBe(false);
  });
});

describe("checkpointListText", () => {
  it("lists the checkpoints in UTC, newest first", () => {
    expect(
      checkpointListText([
        { number: 1, created_at: "2026-11-21T09:30:12+00:00", tool: "", kind: "rollback" },
        { number: 2, created_at: "2026-11-21T15:00:00+05:30", tool: "Bash", kind: "tool" },
      ]),
    ).toBe(
      [
        "Checkpoints of the session files, newest first:",
        "1. 2026-11-21 09:30 UTC - the files before a restore",
        "2. 2026-11-21 09:30 UTC - before Bash",
        "Send /rollback NUMBER to restore one. /undo restores the files before the last run.",
      ].join("\n"),
    );
  });

  it("says when there are no checkpoints", () => {
    expect(checkpointListText([])).toBe(NO_CHECKPOINTS);
  });
});
