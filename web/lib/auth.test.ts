import { describe, expect, it, vi } from "vitest";
import { AUTH_KEY, AuthSession } from "./auth";
import type { KeyValueStore } from "./pkce";

function store(): KeyValueStore {
  const data = new Map<string, string>();
  return {
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  };
}

function reply(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function make(fetcher: ReturnType<typeof vi.fn>, now = () => 1_000_000) {
  const session = store();
  const local = store();
  const auth = new AuthSession({
    config: { url: "https://x.supabase.co", anonKey: "anon" },
    session,
    local,
    fetcher: fetcher as never,
    now,
  });
  return { auth, session, local };
}

describe("AuthSession", () => {
  it("keeps the tokens of a password sign-in in sessionStorage", async () => {
    const fetcher = vi.fn(async () =>
      reply({ access_token: "a1", refresh_token: "r1", expires_in: 3600 }),
    );
    const { auth, session } = make(fetcher);
    await auth.signInWithPassword("a@b.c", "pw");
    expect((fetcher.mock.calls[0] as unknown[] | undefined)?.[0]).toBe(
      "https://x.supabase.co/auth/v1/token?grant_type=password",
    );
    expect(JSON.parse(session.getItem(AUTH_KEY) ?? "null")).toEqual({
      access_token: "a1",
      refresh_token: "r1",
      expires_at: 1_000_000 + 3_600_000,
    });
    expect(await auth.getToken()).toBe("a1");
  });

  it("shows the error text of the auth server", async () => {
    const { auth } = make(vi.fn(async () => reply({ error_description: "Invalid login" }, 400)));
    await expect(auth.signInWithPassword("a@b.c", "bad")).rejects.toThrow("Invalid login");
  });

  it("refreshes a token that is about to expire, one refresh for parallel calls", async () => {
    let now = 1_000_000;
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(reply({ access_token: "a1", refresh_token: "r1", expires_in: 100 }))
      .mockResolvedValueOnce(reply({ access_token: "a2", refresh_token: "r2", expires_in: 3600 }));
    const { auth } = make(fetcher, () => now);
    await auth.signInWithPassword("a@b.c", "pw");
    now += 50_000; // 50 s left: inside the 60 s margin
    const [x, y] = await Promise.all([auth.getToken(), auth.getToken()]);
    expect([x, y]).toEqual(["a2", "a2"]);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(JSON.parse(fetcher.mock.calls[1]?.[1].body)).toEqual({ refresh_token: "r1" });
  });

  it("signs out when the refresh fails", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(reply({ access_token: "a1", refresh_token: "r1", expires_in: 10 }))
      .mockResolvedValueOnce(reply({ msg: "Invalid Refresh Token" }, 400));
    const { auth, session } = make(fetcher);
    await auth.signInWithPassword("a@b.c", "pw");
    expect(await auth.refresh(auth.generation)).toBeNull();
    expect(session.getItem(AUTH_KEY)).toBeNull();
    expect(auth.current).toBeNull();
  });

  it("does not use a sign-in link that this browser did not ask for", async () => {
    const fetcher = vi.fn();
    const { auth } = make(fetcher);
    await expect(auth.exchangeCode("code")).rejects.toThrow(/not from this browser/);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("sends the challenge with the link request and the verifier with the code", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(reply({}))
      .mockResolvedValueOnce(reply({ access_token: "a1", expires_in: 3600 }));
    const { auth } = make(fetcher);
    await auth.sendSignInLink("a@b.c", "https://app.test/login");
    expect((fetcher.mock.calls[0] as unknown[] | undefined)?.[0]).toBe(
      "https://x.supabase.co/auth/v1/otp?redirect_to=https%3A%2F%2Fapp.test%2Flogin",
    );
    const otp = JSON.parse(fetcher.mock.calls[0]?.[1].body);
    expect(otp).toMatchObject({ create_user: false, code_challenge_method: "s256" });
    await auth.exchangeCode("abc");
    const exchange = JSON.parse(fetcher.mock.calls[1]?.[1].body);
    expect(exchange.auth_code).toBe("abc");
    expect(typeof exchange.code_verifier).toBe("string");
    await expect(auth.exchangeCode("abc")).rejects.toThrow(/not from this browser/);
  });
  it("signs up with PKCE: a confirm link comes back as a code for this browser", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(reply({ id: "u1", email: "a@b.c" }))
      .mockResolvedValueOnce(reply({ access_token: "a1", refresh_token: "r1", expires_in: 3600 }));
    const { auth, session } = make(fetcher);
    expect(await auth.signUp("a@b.c", "long-password", "https://app.test/login")).toBe("confirm");
    expect((fetcher.mock.calls[0] as unknown[] | undefined)?.[0]).toBe(
      "https://x.supabase.co/auth/v1/signup?redirect_to=https%3A%2F%2Fapp.test%2Flogin",
    );
    const body = JSON.parse(fetcher.mock.calls[0]?.[1].body);
    expect(body).toMatchObject({ email: "a@b.c", password: "long-password", code_challenge_method: "s256" });
    expect(typeof body.code_challenge).toBe("string");
    expect(session.getItem(AUTH_KEY)).toBeNull();
    await auth.exchangeCode("abc");
    expect(await auth.getToken()).toBe("a1");
  });

  it("signs in at once when the project does not ask for a confirmed email", async () => {
    const fetcher = vi.fn(async () =>
      reply({ access_token: "a1", refresh_token: "r1", expires_in: 3600, user: { id: "u1" } }),
    );
    const { auth } = make(fetcher);
    expect(await auth.signUp("a@b.c", "long-password", "https://app.test/login")).toBe("signed-in");
    expect(await auth.getToken()).toBe("a1");
  });

  it("shows the error text of a failed sign-up", async () => {
    const { auth } = make(vi.fn(async () => reply({ msg: "Signups not allowed for this instance" }, 422)));
    await expect(auth.signUp("a@b.c", "long-password", "https://app.test/login")).rejects.toThrow(
      "Signups not allowed",
    );
  });
});

/**
 * A fake BroadcastChannel bus. Messages wait in a queue until `flush` (or `deliver` for one), so
 * a test can put them in any order, as a real browser can.
 */
function bus() {
  type Tab = { onmessage: ((event: { data: unknown }) => void) | null; postMessage: (m: unknown) => void };
  const tabs: Tab[] = [];
  const queue: { to: Tab; data: unknown }[] = [];
  const open = (): Tab => {
    const tab: Tab = {
      onmessage: null,
      postMessage(message: unknown) {
        for (const other of tabs) if (other !== tab) queue.push({ to: other, data: structuredClone(message) });
      },
    };
    tabs.push(tab);
    return tab;
  };
  const deliver = (index = 0) => {
    const [item] = queue.splice(index, 1);
    item?.to.onmessage?.({ data: item.data });
  };
  const flush = async () => {
    while (queue.length > 0) {
      deliver();
      await Promise.resolve();
    }
  };
  return { open, queue, deliver, flush };
}

function tab(channel: ReturnType<ReturnType<typeof bus>["open"]>, fetcher = vi.fn()) {
  const session = store();
  const auth = new AuthSession({
    config: { url: "https://x.supabase.co", anonKey: "anon" },
    session,
    local: store(),
    fetcher: fetcher as never,
    now: () => 1_000_000,
    tabs: channel,
  });
  return { auth, session };
}

function counter() {
  let n = 0;
  return vi.fn(async () => {
    n += 1;
    return reply({ access_token: `a${n}`, refresh_token: `r${n}`, expires_in: 3600 });
  });
}

/** Asks the open tabs and delivers the messages while the new tab waits. */
async function adopt(tabs: ReturnType<typeof bus>, newTab: ReturnType<typeof tab>) {
  const asked = newTab.auth.adoptFromTabs(1000);
  await tabs.flush();
  return asked;
}

describe("AuthSession across tabs (M8.1)", () => {
  it("a new tab opens signed in with the tokens of an open tab", async () => {
    const tabs = bus();
    const first = tab(tabs.open(), counter());
    await first.auth.signInWithPassword("a@b.c", "pw");
    const second = tab(tabs.open());
    expect(second.auth.load()).toBeNull();
    const adopted = await adopt(tabs, second);
    expect(adopted?.access_token).toBe("a1");
    expect(JSON.parse(second.session.getItem(AUTH_KEY) ?? "null").refresh_token).toBe("r1");
    expect(await second.auth.getToken()).toBe("a1");
  });

  it("a tab alone waits and stays signed out", async () => {
    const tabs = bus();
    const lone = tab(tabs.open());
    expect(await lone.auth.adoptFromTabs(10)).toBeNull();
  });

  it("a refresh and a sign-out reach the tabs of the same sign-in", async () => {
    const tabs = bus();
    const first = tab(tabs.open(), counter());
    await first.auth.signInWithPassword("a@b.c", "pw");
    const second = tab(tabs.open());
    await adopt(tabs, second);
    await first.auth.refresh(first.auth.generation);
    await tabs.flush();
    expect(second.auth.current?.refresh_token).toBe("r2");
    first.auth.signOut();
    await tabs.flush();
    expect(second.auth.current).toBeNull();
    expect(second.session.getItem(AUTH_KEY)).toBeNull();
  });

  it("a late refresh never brings old tokens back", async () => {
    const tabs = bus();
    const first = tab(tabs.open(), counter());
    await first.auth.signInWithPassword("a@b.c", "pw");
    const second = tab(tabs.open());
    await adopt(tabs, second);
    await first.auth.refresh(first.auth.generation); // rev 1, r2
    await first.auth.refresh(first.auth.generation); // rev 2, r3
    tabs.deliver(1); // the newer refresh first
    tabs.deliver(0); // then the older one
    expect(second.auth.current?.refresh_token).toBe("r3");
  });

  it("a sign-in in one tab does not change the account of another tab", async () => {
    const tabs = bus();
    const first = tab(tabs.open(), counter());
    const second = tab(tabs.open(), counter());
    await first.auth.signInWithPassword("a@b.c", "pw");
    await second.auth.signInWithPassword("other@b.c", "pw");
    const generation = first.auth.generation;
    await second.auth.refresh(second.auth.generation);
    second.auth.signOut();
    await tabs.flush();
    expect(first.auth.current?.access_token).toBe("a1");
    expect(first.auth.generation).toBe(generation);
  });

  it("a token reply that comes after a sign-out is not taken", async () => {
    const tabs = bus();
    const first = tab(tabs.open(), counter());
    await first.auth.signInWithPassword("a@b.c", "pw");
    const third = tab(tabs.open());
    const asked = third.auth.adoptFromTabs(50);
    tabs.deliver(0); // the ask reaches the first tab, which queues its reply
    first.auth.signOut(); // queues the sign-out after the reply
    tabs.deliver(tabs.queue.length - 1); // the sign-out arrives first
    await tabs.flush(); // then the old reply
    expect(await asked).toBeNull();
    expect(third.auth.current).toBeNull();
  });

  it("ignores a message that is not a sign-in message", async () => {
    const channel = { onmessage: null as ((event: { data: unknown }) => void) | null, postMessage: vi.fn() };
    const lone = tab(channel);
    channel.onmessage?.({ data: { type: "tokens", ask: "x", lineage: "l", rev: 1, tokens: { access_token: 3 } } });
    channel.onmessage?.({ data: { type: "refresh", lineage: "l", rev: 1, tokens: { access_token: "a", expires_at: 2e9 } } });
    channel.onmessage?.({ data: "junk" });
    expect(lone.auth.current).toBeNull();
  });
});
