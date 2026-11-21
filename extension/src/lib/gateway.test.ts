import { describe, expect, it } from "vitest";

import { BACKOFF_MS, Gateway, GatewayError, type GatewayStatus, PING_MS, type SocketLike, type Timers } from "./gateway";
import type { ServerFrame } from "./protocol";

class FakeSocket implements SocketLike {
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  closedWith: number | null = null;

  constructor(readonly url: string) {}

  open(): void {
    this.readyState = 1;
    this.onopen?.({} as Event);
  }
  push(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent);
  }
  drop(code: number, reason = ""): void {
    this.readyState = 3;
    this.onclose?.({ code, reason } as CloseEvent);
  }
  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }
  close(code?: number): void {
    this.readyState = 3;
    this.closedWith = code ?? 1000;
  }
}

class FakeTimers implements Timers {
  now = 1_000_000;
  private seq = 0;
  private readonly tasks = new Map<number, { at: number; fn: () => void; every: number | null }>();

  setTimeout(fn: () => void, ms: number): unknown {
    this.seq += 1;
    this.tasks.set(this.seq, { at: this.now + ms, fn, every: null });
    return this.seq;
  }
  clearTimeout(handle: unknown): void {
    this.tasks.delete(handle as number);
  }
  setInterval(fn: () => void, ms: number): unknown {
    this.seq += 1;
    this.tasks.set(this.seq, { at: this.now + ms, fn, every: ms });
    return this.seq;
  }
  clearInterval(handle: unknown): void {
    this.tasks.delete(handle as number);
  }
  /** Moves the clock and runs the timers that are due. */
  async advance(ms: number): Promise<void> {
    const end = this.now + ms;
    for (;;) {
      const due = [...this.tasks.entries()].filter(([, task]) => task.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      const [id, task] = due;
      this.now = task.at;
      if (task.every === null) this.tasks.delete(id);
      else task.at += task.every;
      task.fn();
      await flush();
    }
    this.now = end;
  }
}

const flush = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};

function setup(
  tokens: (string | null)[] = ["t1"],
  refreshes: (string | null)[] = [],
  options: { canRefresh?: boolean; refresh?: () => Promise<string | null> } = {},
) {
  const timers = new FakeTimers();
  const sockets: FakeSocket[] = [];
  const frames: ServerFrame[] = [];
  const statuses: [GatewayStatus, string | null][] = [];
  let ids = 0;
  const gateway = new Gateway({
    url: () => "wss://api.test/channels/chrome/ws",
    token: async () => (tokens.length > 1 ? tokens.shift()! : (tokens[0] ?? null)),
    refresh: options.refresh ?? (async () => refreshes.shift() ?? null),
    canRefresh: () => options.canRefresh ?? false,
    createSocket: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    onFrame: (frame) => frames.push(frame),
    onStatus: (status, detail) => statuses.push([status, detail]),
    now: () => timers.now,
    timers,
    newId: () => `id-${++ids}`,
  });
  const last = () => sockets.at(-1)!;
  const ready = async (expiresAt: number | null = timers.now / 1000 + 3600) => {
    await flush();
    last().open();
    last().push({ type: "ready", protocol: 1, expires_at: expiresAt, max_frame_chars: 6_000_000 });
  };
  return { gateway, timers, sockets, frames, statuses, last, ready };
}

describe("Gateway", () => {
  it("sends the token in the first frame, not in the URL", async () => {
    const { gateway, last, ready, statuses } = setup(["secret-token"]);
    gateway.start();
    await ready();
    expect(last().url).toBe("wss://api.test/channels/chrome/ws");
    expect(last().sent[0]).toEqual({ type: "auth", token: "secret-token" });
    expect(statuses.map(([s]) => s)).toEqual(["connecting", "open"]);
  });

  it("stays signed out without a token", async () => {
    const { gateway, sockets, statuses } = setup([null]);
    gateway.start();
    await flush();
    expect(sockets).toHaveLength(0);
    expect(statuses).toEqual([["signed_out", null]]);
  });

  it("pings to keep the socket and the service worker alive", async () => {
    const { gateway, last, ready, timers } = setup();
    gateway.start();
    await ready();
    await timers.advance(PING_MS * 2);
    expect(last().sent.filter((f) => f.type === "ping")).toHaveLength(2);
  });

  it("follows sessions and resumes them after a reconnect from the last event", async () => {
    const { gateway, last, ready, timers, sockets } = setup();
    gateway.subscribe("s1");
    gateway.start();
    await ready();
    expect(last().sent[1]).toEqual({ type: "subscribe", session_id: "s1" });
    last().push({ type: "subscribed", session_id: "s1", after: 4 });
    last().push({ type: "run_event", session_id: "s1", event_id: 7, data: { type: "text_delta" } });
    last().drop(1006);
    await flush();
    expect(gateway.status).toBe("reconnecting");
    await timers.advance(BACKOFF_MS[0]);
    expect(sockets).toHaveLength(2);
    await ready();
    expect(last().sent[1]).toEqual({ type: "subscribe", session_id: "s1", after: 7 });
  });

  it("follows a session again after its stream ends", async () => {
    const { gateway, last, ready, timers } = setup();
    gateway.start();
    await ready();
    gateway.subscribe("s1", 3);
    last().push({ type: "end", session_id: "s1", data: {} });
    await timers.advance(1_000);
    expect(last().sent.filter((f) => f.type === "subscribe")).toEqual([
      { type: "subscribe", session_id: "s1", after: 3 },
      { type: "subscribe", session_id: "s1", after: 3 },
    ]);
  });

  it("stops following a session that is not found", async () => {
    const { gateway, last, ready } = setup();
    gateway.start();
    await ready();
    gateway.subscribe("gone");
    last().push({ type: "error", code: "not_found", message: "no", session_id: "gone" });
    expect(gateway.followed.has("gone")).toBe(false);
  });

  it("sends a fresh token before the old one expires", async () => {
    const { gateway, last, ready, timers } = setup(["t1", "t2"]);
    gateway.start();
    await ready(timers.now / 1000 + 120);
    await timers.advance(60_000);
    expect(last().sent.filter((f) => f.type === "auth")).toEqual([
      { type: "auth", token: "t1" },
      { type: "auth", token: "t2" },
    ]);
  });

  it("resolves a send with the run and rejects it with the error of the server", async () => {
    const { gateway, last, ready } = setup();
    gateway.start();
    await ready();
    const ok = gateway.send("s1", "hello");
    expect(last().sent.at(-1)).toEqual({ type: "send", id: "id-1", session_id: "s1", text: "hello" });
    last().push({ type: "sent", id: "id-1", session_id: "s1", run_id: "r1", queued: false });
    await expect(ok).resolves.toMatchObject({ run_id: "r1" });

    const bad = gateway.send("s1", "x");
    last().push({ type: "error", id: "id-2", code: "rate_limited", message: "slow", retry_after_s: 3 });
    await expect(bad).rejects.toMatchObject({ code: "rate_limited", retryAfterS: 3 });
  });

  it("rejects a send when it is not connected or the socket closes", async () => {
    const { gateway, last, ready } = setup();
    await expect(gateway.send("s1", "x")).rejects.toBeInstanceOf(GatewayError);
    gateway.start();
    await ready();
    const waiting = gateway.send("s1", "x");
    last().drop(1006);
    await expect(waiting).rejects.toMatchObject({ code: "disconnected" });
  });

  it("refreshes the token after 4401 and signs out when no new token comes", async () => {
    const one = setup(["t1"], ["t2"]);
    one.gateway.start();
    await one.ready();
    one.last().drop(4401, "the access token expired");
    await flush();
    await one.timers.advance(BACKOFF_MS[0]);
    expect(one.sockets).toHaveLength(2);

    const two = setup(["t1"], [null]);
    two.gateway.start();
    await two.ready();
    two.last().drop(4401, "the access token expired");
    await flush();
    expect(two.gateway.status).toBe("signed_out");
    expect(two.statuses.at(-1)?.[1]).toContain("Sign in again");
  });

  it("tries again later when the sign-in service does not answer", async () => {
    const { gateway, last, ready, timers, sockets } = setup(["t1"], [null], { canRefresh: true });
    gateway.start();
    await ready();
    last().drop(4401, "the access token expired");
    await flush();
    expect(gateway.status).toBe("reconnecting");
    await timers.advance(BACKOFF_MS[0]);
    expect(sockets).toHaveLength(2);
  });

  it("ignores the refresh of a run that stopped", async () => {
    let finish: (token: string | null) => void = () => undefined;
    const refresh = () => new Promise<string | null>((resolve) => (finish = resolve));
    const { gateway, last, ready, sockets } = setup(["t1"], [], { refresh });
    gateway.start();
    await ready();
    last().drop(4401, "the access token expired");
    await flush();
    gateway.restart();
    await ready();
    finish(null);
    await flush();
    expect(gateway.status).toBe("open");
    expect(sockets).toHaveLength(2);
  });

  it("tries at once when a panel opens during the wait", async () => {
    const { gateway, last, ready, sockets } = setup();
    gateway.start();
    await ready();
    last().drop(1006, "");
    await flush();
    expect(sockets).toHaveLength(1);
    gateway.start();
    await flush();
    expect(sockets).toHaveLength(2);
  });

  it("does not reconnect when the API refuses the extension", async () => {
    const { gateway, last, ready, timers, sockets } = setup();
    gateway.start();
    await ready();
    last().drop(4403, "origin");
    await flush();
    await timers.advance(60_000);
    expect(sockets).toHaveLength(1);
    expect(gateway.status).toBe("refused");
  });

  it("closes the socket on stop and forgets the sessions", async () => {
    const { gateway, last, ready } = setup();
    gateway.start();
    await ready();
    gateway.subscribe("s1");
    gateway.stop();
    expect(last().closedWith).toBe(1000);
    expect(gateway.followed.size).toBe(0);
    expect(gateway.status).toBe("signed_out");
  });
});
