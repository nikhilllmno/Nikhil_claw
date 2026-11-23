import { describe, expect, it } from "vitest";
import { browserStep, shortUrl } from "./liveBrowser";

const call = (name: string, args: Record<string, unknown>, status: "running" | "done" = "running") => ({
  callId: name,
  name,
  arguments: args,
  status,
  preview: "",
  order: 1,
  seq: 1,
});

describe("browserStep", () => {
  it("shows the newest browser call of the run", () => {
    const live = {
      tools: { r1: [call("mcp__sama__web_search", { query: "x" }), call("mcp__sama__browser_open", { url: "https://booking.com" })] },
      subagents: {},
    };
    expect(browserStep(live, "r1")).toContain("booking.com");
    expect(browserStep(live, "r2")).toBeNull();
  });

  it("shows the newest step of a running browser task", () => {
    const live = {
      tools: { r1: [call("mcp__sama__browser_task", { goal: "find a price" })] },
      subagents: {
        r1: [
          { seq: 1, subagent: "browser_task", text: "started: find a price", callId: "browser_task:1", done: false, tokens: null },
          { seq: 2, subagent: "browser_task", text: "step 2/15: click e12", callId: "browser_task:1", done: false, tokens: null },
        ],
      },
    };
    expect(browserStep(live, "r1")).toBe("Browser task: step 2/15: click e12");
  });

  it("marks a finished call as done", () => {
    const live = { tools: { r1: [call("mcp__sama__browser_snapshot", {}, "done")] }, subagents: {} };
    expect(browserStep(live, "r1")).toMatch(/\(done\)$/);
  });
});

describe("addresses", () => {
  it("shortens a URL for the address line", () => {
    expect(shortUrl("https://www.booking.com/searchresults.html?ss=x")).toBe("www.booking.com/searchresults.html");
    expect(shortUrl("https://example.com/")).toBe("example.com");
    expect(shortUrl(null)).toBe("");
  });
});
