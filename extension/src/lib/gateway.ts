// The one WebSocket of the extension to the sama API. The service worker owns it.
// - The first frame is `auth`. The token is never in the URL.
// - A ping every 20 s keeps the socket and the service worker alive (Chrome 116 and later).
// - Before the token expires, a fresh token goes over the open socket (`auth` again).
// - After a disconnect, the gateway connects again with a backoff and follows the same
//   sessions from the last event it saw, so no event is lost.

import {
  CLOSE,
  type ClientFrame,
  type PageContext,
  parseServerFrame,
  type SentFrame,
  type ServerFrame,
} from "./protocol";

export const PING_MS = 20_000;
export const REAUTH_MARGIN_MS = 60_000;
export const SEND_TIMEOUT_MS = 30_000;
export const BACKOFF_MS = [1_000, 2_000, 5_000, 10_000, 30_000] as const;
/** After an `end` event (the stream of a session reached its time limit), follow it again. */
export const RESUBSCRIBE_MS = 500;

export type GatewayStatus = "signed_out" | "connecting" | "open" | "reconnecting" | "refused";

/** The part of WebSocket that the gateway uses (tests give a fake). */
export interface SocketLike {
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  readonly readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
  setInterval(fn: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
}

export interface GatewayDeps {
  url: () => string;
  /** The access token to use now, or `null` when the user is signed out. */
  token: () => Promise<string | null>;
  /** A new access token after the server refused the old one, or `null`. */
  refresh: () => Promise<string | null>;
  createSocket: (url: string) => SocketLike;
  onFrame: (frame: ServerFrame) => void;
  onStatus: (status: GatewayStatus, detail: string | null) => void;
  now?: () => number;
  timers?: Timers;
  newId?: () => string;
}

export class GatewayError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly retryAfterS: number | null = null,
  ) {
    super(message);
    this.name = "GatewayError";
  }
}

interface Pending {
  resolve: (frame: SentFrame) => void;
  reject: (error: GatewayError) => void;
  timer: unknown;
}

const OPEN = 1;

export class Gateway {
  private socket: SocketLike | null = null;
  private token: string | null = null;
  private running = false;
  private attempt = 0;
  private retryTimer: unknown = null;
  private pingTimer: unknown = null;
  private reauthTimer: unknown = null;
  private ready = false;
  /** The followed sessions and the last event id of each (`undefined`: none yet). */
  private readonly wanted = new Map<string, number | undefined>();
  private readonly pending = new Map<string, Pending>();
  private readonly now: () => number;
  private readonly timers: Timers;
  private readonly newId: () => string;
  private statusValue: GatewayStatus = "signed_out";

  constructor(private readonly deps: GatewayDeps) {
    this.now = deps.now ?? (() => Date.now());
    this.timers = deps.timers ?? {
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
      setInterval: (fn, ms) => setInterval(fn, ms),
      clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
    };
    this.newId = deps.newId ?? (() => crypto.randomUUID());
  }

  get status(): GatewayStatus {
    return this.statusValue;
  }

  get followed(): ReadonlyMap<string, number | undefined> {
    return this.wanted;
  }

  private setStatus(status: GatewayStatus, detail: string | null = null): void {
    this.statusValue = status;
    this.deps.onStatus(status, detail);
  }

  /** Connects if the user is signed in. Does nothing if the gateway runs already. */
  start(): void {
    if (this.running) return;
    this.running = true;
    this.attempt = 0;
    void this.connect();
  }

  /** Closes the socket and forgets the sessions (sign-out or a new API address). */
  stop(status: GatewayStatus = "signed_out"): void {
    this.running = false;
    this.wanted.clear();
    this.closeSocket(1000, "stopped");
    this.setStatus(status);
  }

  /** Stops and starts again (a new sign-in). */
  restart(): void {
    this.stop();
    this.start();
  }

  private async connect(): Promise<void> {
    this.clearTimer("retry");
    const token = await this.deps.token();
    if (!this.running) return;
    if (token === null) {
      this.running = false;
      this.setStatus("signed_out");
      return;
    }
    this.token = token;
    this.ready = false;
    this.setStatus(this.attempt === 0 ? "connecting" : "reconnecting");
    let socket: SocketLike;
    try {
      socket = this.deps.createSocket(this.deps.url());
    } catch {
      this.retry();
      return;
    }
    this.socket = socket;
    socket.onopen = () => this.write({ type: "auth", token });
    socket.onmessage = (event) => {
      if (this.socket === socket && typeof event.data === "string") this.receive(event.data);
    };
    socket.onclose = (event) => {
      if (this.socket === socket) void this.closed(event.code, event.reason);
    };
    socket.onerror = () => undefined; // onclose follows.
  }

  private write(frame: ClientFrame): boolean {
    const socket = this.socket;
    if (socket === null || socket.readyState !== OPEN) return false;
    socket.send(JSON.stringify(frame));
    return true;
  }

  private receive(text: string): void {
    const frame = parseServerFrame(text);
    if (frame === null) return;
    switch (frame.type) {
      case "ready":
        this.ready = true;
        this.attempt = 0;
        this.setStatus("open");
        this.scheduleReauth(frame.expires_at);
        this.clearTimer("ping");
        this.pingTimer = this.timers.setInterval(() => this.write({ type: "ping" }), PING_MS);
        for (const [sessionId, after] of this.wanted) this.writeSubscribe(sessionId, after);
        break;
      case "auth_ok":
        this.scheduleReauth(frame.expires_at);
        break;
      case "runs":
      case "run_event":
      case "run":
        if (frame.event_id !== undefined && this.wanted.has(frame.session_id)) {
          this.wanted.set(frame.session_id, frame.event_id);
        }
        break;
      case "end": {
        const sessionId = frame.session_id;
        this.timers.setTimeout(() => {
          if (this.ready && this.wanted.has(sessionId)) {
            this.writeSubscribe(sessionId, this.wanted.get(sessionId));
          }
        }, RESUBSCRIBE_MS);
        break;
      }
      case "subscribed":
        if (this.wanted.has(frame.session_id) && this.wanted.get(frame.session_id) === undefined) {
          this.wanted.set(frame.session_id, frame.after);
        }
        break;
      case "sent":
        this.settle(frame.id, (pending) => pending.resolve(frame));
        break;
      case "error":
        if (frame.id !== undefined) {
          const error = new GatewayError(frame.code, frame.message, frame.retry_after_s ?? null);
          this.settle(frame.id, (pending) => pending.reject(error));
        } else if (frame.session_id !== undefined && frame.code !== "rate_limited") {
          // The session is gone or hidden: stop following it.
          this.wanted.delete(frame.session_id);
        }
        break;
      default:
        break;
    }
    this.deps.onFrame(frame);
  }

  private settle(id: string, action: (pending: Pending) => void): void {
    const pending = this.pending.get(id);
    if (pending === undefined) return;
    this.pending.delete(id);
    this.timers.clearTimeout(pending.timer);
    action(pending);
  }

  private scheduleReauth(expiresAt: number | null): void {
    this.clearTimer("reauth");
    if (expiresAt === null) return;
    const wait = Math.max(1_000, expiresAt * 1000 - REAUTH_MARGIN_MS - this.now());
    this.reauthTimer = this.timers.setTimeout(() => void this.reauth(), wait);
  }

  private async reauth(): Promise<void> {
    const token = await this.deps.token();
    // A token that did not change cannot move the end. The server closes the socket at the
    // end, and `closed` asks for a new token.
    if (token !== null && token !== this.token && this.ready) {
      this.token = token;
      this.write({ type: "auth", token });
    }
  }

  private async closed(code: number, reason: string): Promise<void> {
    this.socket = null;
    this.ready = false;
    this.clearTimer("ping");
    this.clearTimer("reauth");
    for (const [id, pending] of this.pending) {
      this.timers.clearTimeout(pending.timer);
      pending.reject(new GatewayError("disconnected", "The connection to sama closed. Try again."));
      this.pending.delete(id);
    }
    if (!this.running) return;
    if (code === CLOSE.forbidden) {
      this.running = false;
      this.setStatus("refused", reason || "The API does not accept this extension.");
      return;
    }
    if (code === CLOSE.unauthorized) {
      const token = (await this.deps.refresh()) ?? (await this.deps.token());
      if (!this.running) return;
      if (token === null || token === this.token) {
        this.running = false;
        this.setStatus("signed_out", "Your sign-in ended. Sign in again.");
        return;
      }
    }
    this.retry();
  }

  private retry(): void {
    if (!this.running) return;
    const wait = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)] ?? 30_000;
    this.attempt += 1;
    this.setStatus("reconnecting");
    this.clearTimer("retry");
    this.retryTimer = this.timers.setTimeout(() => void this.connect(), wait);
  }

  private closeSocket(code: number, reason: string): void {
    const socket = this.socket;
    this.socket = null;
    this.ready = false;
    this.clearTimer("ping");
    this.clearTimer("reauth");
    this.clearTimer("retry");
    if (socket !== null) {
      socket.onclose = null;
      socket.onmessage = null;
      try {
        socket.close(code, reason);
      } catch {
        // Already closed.
      }
    }
    for (const pending of this.pending.values()) {
      this.timers.clearTimeout(pending.timer);
      pending.reject(new GatewayError("disconnected", "The connection to sama closed."));
    }
    this.pending.clear();
  }

  private clearTimer(name: "retry" | "ping" | "reauth"): void {
    if (name === "retry" && this.retryTimer !== null) {
      this.timers.clearTimeout(this.retryTimer);
      this.retryTimer = null;
    } else if (name === "ping" && this.pingTimer !== null) {
      this.timers.clearInterval(this.pingTimer);
      this.pingTimer = null;
    } else if (name === "reauth" && this.reauthTimer !== null) {
      this.timers.clearTimeout(this.reauthTimer);
      this.reauthTimer = null;
    }
  }

  private writeSubscribe(sessionId: string, after: number | undefined): void {
    this.write(
      after === undefined
        ? { type: "subscribe", session_id: sessionId }
        : { type: "subscribe", session_id: sessionId, after },
    );
  }

  /** Follows the events of a session. With `after`, the events after that event id first. */
  subscribe(sessionId: string, after?: number): void {
    const known = this.wanted.get(sessionId);
    const cursor = after ?? known;
    this.wanted.set(sessionId, cursor);
    if (this.ready) this.writeSubscribe(sessionId, cursor);
  }

  unsubscribe(sessionId: string): void {
    if (!this.wanted.delete(sessionId)) return;
    if (this.ready) this.write({ type: "unsubscribe", session_id: sessionId });
  }

  /** Sends a message, with the page for "Ask about this page". Resolves with the run. */
  send(sessionId: string, text: string, page?: PageContext): Promise<SentFrame> {
    if (!this.ready) {
      return Promise.reject(new GatewayError("disconnected", "sama is not connected. Wait a moment."));
    }
    const id = this.newId();
    return new Promise<SentFrame>((resolve, reject) => {
      const timer = this.timers.setTimeout(() => {
        this.pending.delete(id);
        reject(new GatewayError("timeout", "sama did not answer in time. Try again."));
      }, SEND_TIMEOUT_MS);
      this.pending.set(id, { resolve, reject, timer });
      const frame: ClientFrame = page
        ? { type: "send", id, session_id: sessionId, text, page }
        : { type: "send", id, session_id: sessionId, text };
      if (!this.write(frame)) {
        this.settle(id, (pending) =>
          pending.reject(new GatewayError("disconnected", "sama is not connected. Wait a moment.")),
        );
      }
    });
  }
}
