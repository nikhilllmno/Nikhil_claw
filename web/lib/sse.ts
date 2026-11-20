// A parser for server-sent events (the text/event-stream format).
// The browser EventSource cannot send the Authorization header, so the app reads the stream
// with fetch and gives the text chunks to this parser.

export interface SseEvent {
  /** The `id` field, or `null` if the event has none. */
  id: string | null;
  /** The `event` field. `message` if the event has none. */
  event: string;
  /** The `data` lines, joined with a line feed. */
  data: string;
  /** The `retry` field in milliseconds, if the event has a valid one. */
  retry: number | null;
}

/** Incremental parser. Call `push` with each chunk of text, in order. */
export class SseParser {
  private buffer = "";
  private pendingCr = false;

  /** Returns the events that the chunk completes. A partial event stays in the buffer. */
  push(chunk: string): SseEvent[] {
    let text = chunk;
    if (this.pendingCr) {
      // The last chunk ended with CR. If a LF follows, it is the second half of CRLF.
      this.pendingCr = false;
      if (text.startsWith("\n")) text = text.slice(1);
    }
    if (text.endsWith("\r")) this.pendingCr = true;
    this.buffer += text.replace(/\r\n|\r/g, "\n");
    const events: SseEvent[] = [];
    let end: number;
    while ((end = this.buffer.indexOf("\n\n")) >= 0) {
      const block = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      const event = parseBlock(block);
      if (event !== null) events.push(event);
    }
    return events;
  }
}

/** Parses one block of lines (without the blank line at its end). `null`: nothing to dispatch. */
export function parseBlock(block: string): SseEvent | null {
  let id: string | null = null;
  let name = "";
  let retry: number | null = null;
  const data: string[] = [];
  let hasData = false;
  for (const line of block.split("\n")) {
    if (line === "" || line.startsWith(":")) continue;
    const at = line.indexOf(":");
    const field = at < 0 ? line : line.slice(0, at);
    const value = at < 0 ? "" : line.slice(at + 1).replace(/^ /, "");
    if (field === "id") {
      if (!value.includes("\0")) id = value;
    } else if (field === "event") {
      name = value;
    } else if (field === "data") {
      data.push(value);
      hasData = true;
    } else if (field === "retry" && /^\d+$/.test(value)) {
      retry = Number(value);
    }
  }
  // An event with only `retry` or `id` fields still carries them to the caller.
  if (!hasData && id === null && retry === null && name === "") return null;
  return { id, event: name || "message", data: data.join("\n"), retry };
}

/** The `data` of an event as JSON, or `null` if it is empty or not valid JSON. */
export function eventJson(event: SseEvent): unknown {
  if (event.data === "") return null;
  try {
    return JSON.parse(event.data) as unknown;
  } catch {
    return null;
  }
}
