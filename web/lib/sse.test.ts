import { describe, expect, it } from "vitest";
import { eventJson, parseBlock, SseParser } from "./sse";

describe("SseParser", () => {
  it("parses id, event, and data", () => {
    const events = new SseParser().push('id: 7\nevent: run_event\ndata: {"a":1}\n\n');
    expect(events).toEqual([{ id: "7", event: "run_event", data: '{"a":1}' }]);
  });

  it("uses message as the default event name", () => {
    expect(new SseParser().push("data: hi\n\n")[0]?.event).toBe("message");
  });

  it("joins data lines with a line feed", () => {
    expect(new SseParser().push("data: a\ndata: b\n\n")[0]?.data).toBe("a\nb");
  });

  it("keeps a partial event until it is complete", () => {
    const parser = new SseParser();
    expect(parser.push("event: run\nda")).toEqual([]);
    expect(parser.push('ta: {"x":')).toEqual([]);
    const events = parser.push("1}\n\nevent: next\n");
    expect(events).toHaveLength(1);
    expect(eventJson(events[0]!)).toEqual({ x: 1 });
    const rest = parser.push("data: 2\n\n");
    expect(rest[0]?.event).toBe("next");
  });

  it("returns several events from one chunk", () => {
    const events = new SseParser().push("data: 1\n\ndata: 2\n\ndata: 3\n\n");
    expect(events.map((e) => e.data)).toEqual(["1", "2", "3"]);
  });

  it("accepts CRLF and CR line ends, also split between chunks", () => {
    const parser = new SseParser();
    expect(parser.push("data: a\r\n\r\n")[0]?.data).toBe("a");
    expect(parser.push("data: b\r")).toEqual([]);
    expect(parser.push("\n\r\n")[0]?.data).toBe("b");
    expect(parser.push("data: c\r\rdata: d\r\r").map((e) => e.data)).toEqual(["c", "d"]);
  });

  it("ignores comments such as the heartbeat", () => {
    expect(new SseParser().push(": ping\n\n")).toEqual([]);
    expect(new SseParser().push(": ping\ndata: x\n\n")[0]?.data).toBe("x");
  });

  it("removes only one space after the colon", () => {
    expect(new SseParser().push("data:  two\n\n")[0]?.data).toBe(" two");
    expect(new SseParser().push("data:none\n\n")[0]?.data).toBe("none");
  });

  it("ignores the retry field", () => {
    expect(new SseParser().push("retry: 2000\n\n")).toEqual([]);
  });

  it("treats a field without a colon as a field with an empty value", () => {
    expect(new SseParser().push("data\n\n")[0]?.data).toBe("");
  });
});

describe("parseBlock and eventJson", () => {
  it("returns null for an empty block", () => {
    expect(parseBlock("")).toBeNull();
  });

  it("returns null JSON for empty or invalid data", () => {
    expect(eventJson({ id: null, event: "x", data: "" })).toBeNull();
    expect(eventJson({ id: null, event: "x", data: "{nope" })).toBeNull();
  });
});
