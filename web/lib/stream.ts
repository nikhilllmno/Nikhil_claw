// The event stream of a session: a streamed fetch that reconnects with backoff and keeps the
// cursor (`after`), so no event is lost or shown twice.

import type { ApiClient } from "./api";
import { ApiError } from "./api";
import { eventJson, SseParser } from "./sse";

export interface StreamOptions {
  api: Pick<ApiClient, "raw">;
  sessionId: string;
  /** The `last_event_id` of the session detail. */
  after: number;
  signal: AbortSignal;
  onEvent: (name: string, data: unknown) => void;
  /** `true` while the stream is connected, `false` while it tries again. */
  onConnection?: (connected: boolean, error: string | null) => void;
  /** Delay before try number `attempt` (0 for the first retry), in milliseconds. */
  backoff?: (attempt: number) => number;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

export const defaultBackoff = (attempt: number): number => Math.min(15_000, 1_000 * 2 ** attempt);

export function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done);
  });
}

/**
 * Reads the stream until `signal` aborts or the server sends `end`. A closed connection (the
 * server ends a stream after some time) opens again at once. An error opens it again after a
 * backoff. A 4xx other than 429 is final: the session is gone or not visible.
 */
export async function runEventStream(options: StreamOptions): Promise<void> {
  const { api, sessionId, signal, onEvent, onConnection } = options;
  const backoff = options.backoff ?? defaultBackoff;
  const wait = options.sleep ?? sleep;
  let cursor = options.after;
  let attempt = 0;
  while (!signal.aborted) {
    try {
      const response = await api.raw(`/sessions/${sessionId}/events?after=${cursor}`, { signal });
      if (!response.ok || response.body === null) {
        if (response.status >= 400 && response.status < 500 && response.status !== 429) {
          onConnection?.(false, `The event stream failed (${response.status}).`);
          return;
        }
        throw new Error(`The event stream failed (${response.status}).`);
      }
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      const parser = new SseParser();
      onConnection?.(true, null);
      const startedAt = Date.now();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const event of parser.push(value)) {
          if (event.id !== null && /^\d+$/.test(event.id)) cursor = Number(event.id);
          if (event.event === "end") {
            onConnection?.(false, null);
            return;
          }
          const data = eventJson(event);
          if (data !== null) onEvent(event.event, data);
        }
      }
      // The server closed the stream after its time limit: connect again at once from the
      // cursor. A stream that closes at once is an error: use the backoff.
      if (Date.now() - startedAt > 1_000) {
        attempt = 0;
        continue;
      }
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) {
        onConnection?.(false, error.message);
        return; // The client signed the user out.
      }
      onConnection?.(false, error instanceof Error ? error.message : "The event stream failed.");
    }
    await wait(backoff(attempt), signal);
    attempt += 1;
  }
}
