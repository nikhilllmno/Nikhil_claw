import { describe, expect, it } from "vitest";
import type { TraceStep } from "./api";
import {
  formatCost,
  formatDuration,
  formatTokens,
  integrationTone,
  runTone,
  snippetParts,
  stepSummary,
  stepTitle,
  toneBadge,
  toolTone,
} from "./history";

function step(type: string, payload: Record<string, unknown>): TraceStep {
  return { seq: 1, type, created_at: "2026-01-01T00:00:00Z", payload };
}

describe("history helpers", () => {
  it("formats the cost", () => {
    expect(formatCost("0")).toBe("$0");
    expect(formatCost("0.00001")).toBe("<$0.0001");
    expect(formatCost("0.0123")).toBe("$0.0123");
    expect(formatCost("12.5")).toBe("$12.50");
    expect(formatCost("nope")).toBe("$0");
  });

  it("formats the tokens", () => {
    expect(formatTokens(950)).toBe("950");
    expect(formatTokens(1200)).toBe("1.2k");
    expect(formatTokens(2000)).toBe("2k");
    expect(formatTokens(3_400_000)).toBe("3.4M");
  });

  it("formats the duration", () => {
    expect(formatDuration(null, "2026-01-01T00:00:00Z")).toBe("");
    expect(formatDuration("2026-01-01T00:00:00Z", "2026-01-01T00:00:00.250Z")).toBe("250 ms");
    expect(formatDuration("2026-01-01T00:00:00Z", "2026-01-01T00:00:02.5Z")).toBe("2.5 s");
    expect(formatDuration("2026-01-01T00:00:00Z", "2026-01-01T00:02:05Z")).toBe("2 min 5 s");
  });

  it("gives a tone for each run status", () => {
    expect(runTone("completed")).toBe("ok");
    expect(runTone("running")).toBe("wait");
    expect(runTone("failed")).toBe("error");
    expect(runTone("cancelled")).toBe("off");
    expect(toolTone("finished")).toBe("ok");
    expect(toolTone("denied")).toBe("error");
  });

  it("describes the steps", () => {
    expect(stepTitle(step("text", {}))).toBe("Answer");
    expect(stepTitle(step("something_new", {}))).toBe("something new");
    expect(stepSummary(step("text", { text: "hi" }))).toBe("hi");
    expect(stepSummary(step("tool_call_started", { name: "web_search" }))).toBe("web_search");
    expect(stepSummary(step("tool_call_finished", { name: "x", is_error: true }))).toBe("x (error)");
    expect(stepSummary(step("run_failed", { code: "timeout", message: "too slow" }))).toBe(
      "timeout: too slow",
    );
    expect(stepTitle(step("subagent_event", {}))).toBe("Sub-agent step");
    expect(stepSummary(step("subagent_event", { subagent: "web", text: "Open page" }))).toBe("web: Open page");
    expect(stepSummary(step("usage", { n: 1 }))).toBe("");
  });

  it("maps a tone to the badge classes", () => {
    expect(toneBadge("ok")).toBe("badge badge-accent");
    expect(toneBadge("wait")).toBe("badge badge-warning");
    expect(toneBadge("error")).toBe("badge badge-danger");
    expect(toneBadge("off")).toBe("badge");
  });

  it("maps a chat app status to a tone", () => {
    expect(integrationTone(undefined)).toBe("off");
    expect(integrationTone("active")).toBe("ok");
    expect(integrationTone("pending")).toBe("wait");
    expect(integrationTone("failed")).toBe("error");
  });

  it("splits a snippet at the match markers", () => {
    expect(snippetParts("plain")).toEqual([{ text: "plain", match: false }]);
    expect(snippetParts("the <<deploy>> plan <<now>>")).toEqual([
      { text: "the ", match: false },
      { text: "deploy", match: true },
      { text: " plan ", match: false },
      { text: "now", match: true },
    ]);
    expect(snippetParts("<<a>>")).toEqual([{ text: "a", match: true }]);
    expect(snippetParts("")).toEqual([]);
  });

  it("keeps a marker without its pair as text", () => {
    expect(snippetParts("a << b")).toEqual([{ text: "a << b", match: false }]);
    expect(snippetParts("x <<>> y")).toEqual([
      { text: "x ", match: false },
      { text: " y", match: false },
    ]);
  });
});
