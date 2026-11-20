import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { runEventStream } from "./stream";

function sse(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { status: 200 },
  );
}

describe("runEventStream", () => {
  it("delivers events and stops at the end event", async () => {
    const raw = vi.fn(async () =>
      sse(
        'retry: 2000\n\nevent: runs\ndata: {"runs":[]}\n\n',
        'id: 5\nevent: run_event\ndata: {"type":"text_delta"}\n\nevent: end\ndata: {}\n\n',
      ),
    );
    const seen: [string, unknown][] = [];
    await runEventStream({
      api: { raw },
      sessionId: "s1",
      after: 3,
      signal: new AbortController().signal,
      onEvent: (name, data) => seen.push([name, data]),
    });
    expect((raw.mock.calls[0] as unknown[] | undefined)?.[0]).toBe("/sessions/s1/events?after=3");
    expect(seen).toEqual([
      ["runs", { runs: [] }],
      ["run_event", { type: "text_delta" }],
    ]);
  });

  it("connects again after an error from the last cursor, with a backoff", async () => {
    const controller = new AbortController();
    const raw = vi
      .fn()
      .mockResolvedValueOnce(sse('id: 9\nevent: run\ndata: {"a":1}\n\n'))
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(sse("event: end\ndata: {}\n\n"));
    const waits: number[] = [];
    const states: [boolean, string | null][] = [];
    await runEventStream({
      api: { raw },
      sessionId: "s1",
      after: 0,
      signal: controller.signal,
      onEvent: () => undefined,
      onConnection: (connected, error) => states.push([connected, error]),
      backoff: (attempt) => 100 * (attempt + 1),
      sleep: async (ms) => void waits.push(ms),
    });
    // The first stream closes at once (not after a long time), so it also waits.
    expect(waits).toEqual([100, 200]);
    expect(raw.mock.calls.map((call) => call[0])).toEqual([
      "/sessions/s1/events?after=0",
      "/sessions/s1/events?after=9",
      "/sessions/s1/events?after=9",
    ]);
    expect(states).toContainEqual([false, "network"]);
  });

  it("stops for a 404 and for a 401", async () => {
    const notFound = vi.fn(async () => new Response("{}", { status: 404 }));
    const errors: (string | null)[] = [];
    await runEventStream({
      api: { raw: notFound },
      sessionId: "s1",
      after: 0,
      signal: new AbortController().signal,
      onEvent: () => undefined,
      onConnection: (_, error) => errors.push(error),
    });
    expect(notFound).toHaveBeenCalledTimes(1);
    expect(errors).toEqual(["The event stream failed (404)."]);

    const unauthorized = vi.fn(async () => {
      throw new ApiError(401, "Your sign-in ended. Sign in again.");
    });
    await runEventStream({
      api: { raw: unauthorized },
      sessionId: "s1",
      after: 0,
      signal: new AbortController().signal,
      onEvent: () => undefined,
    });
    expect(unauthorized).toHaveBeenCalledTimes(1);
  });

  it("stops at once when the signal aborts", async () => {
    const controller = new AbortController();
    controller.abort();
    const raw = vi.fn();
    await runEventStream({
      api: { raw },
      sessionId: "s1",
      after: 0,
      signal: controller.signal,
      onEvent: () => undefined,
    });
    expect(raw).not.toHaveBeenCalled();
  });
});
