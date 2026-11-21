// The frames of the sama Chrome channel (`/channels/chrome/ws`, src/sama/api/chrome.py).
// Keep this file the same as the server: docs/architecture.md, Section 14.2.

export const PROTOCOL = 1;

/** The page of a tab, for "Ask about this page" (src/sama/gateway/chrome.py, PageContext). */
export interface PageContext {
  url: string;
  title: string;
  text: string;
  /** A `data:image/jpeg;base64,...` URL, or `null`. */
  screenshot: string | null;
}

export type ClientFrame =
  | { type: "auth"; token: string }
  | { type: "subscribe"; session_id: string; after?: number }
  | { type: "unsubscribe"; session_id: string }
  | { type: "send"; id: string; session_id: string; text: string; page?: PageContext }
  | { type: "ping" };

export interface ReadyFrame {
  type: "ready";
  protocol: number;
  /** Epoch seconds, or `null` for a token without an expiry. */
  expires_at: number | null;
  max_frame_chars: number;
}

export interface ErrorFrame {
  type: "error";
  code: string;
  message: string;
  id?: string;
  session_id?: string;
  retry_after_s?: number;
}

export interface SentFrame {
  type: "sent";
  id: string;
  session_id: string;
  run_id: string;
  queued: boolean;
}

/** An event of a followed session: `runs`, `run_event`, `run`, or `end`. */
export interface SessionEventFrame {
  type: "runs" | "run_event" | "run" | "end";
  session_id: string;
  data: Record<string, unknown>;
  event_id?: number;
}

export type ServerFrame =
  | ReadyFrame
  | ErrorFrame
  | SentFrame
  | SessionEventFrame
  | { type: "auth_ok"; expires_at: number | null }
  | { type: "subscribed"; session_id: string; after: number }
  | { type: "unsubscribed"; session_id: string }
  | { type: "pong" };

/** The close codes of the server. */
export const CLOSE = {
  invalid: 4400,
  unauthorized: 4401,
  forbidden: 4403,
  authTimeout: 4408,
  tooMany: 4429,
} as const;

const SERVER_TYPES = new Set([
  "ready",
  "error",
  "sent",
  "runs",
  "run_event",
  "run",
  "end",
  "auth_ok",
  "subscribed",
  "unsubscribed",
  "pong",
]);

/** Reads one text frame of the server. Returns `null` for a frame that is not known. */
export function parseServerFrame(text: string): ServerFrame | null {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return null;
  }
  if (data === null || typeof data !== "object") return null;
  const type = (data as { type?: unknown }).type;
  if (typeof type !== "string" || !SERVER_TYPES.has(type)) return null;
  return data as ServerFrame;
}

/** The WebSocket address of an API address: `https://x` gives `wss://x/channels/chrome/ws`. */
export function socketUrl(apiUrl: string): string {
  const url = new URL(apiUrl);
  url.protocol = url.protocol === "http:" ? "ws:" : "wss:";
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/channels/chrome/ws`;
  url.search = "";
  url.hash = "";
  return url.toString();
}
