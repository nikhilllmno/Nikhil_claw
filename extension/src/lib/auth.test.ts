import { describe, expect, it } from "vitest";

import { Auth, AUTH_KEY, jwtExpiresIn, REFRESH_MARGIN_MS } from "./auth";
import { memoryStore } from "./bindings";

const CONFIG = { url: "https://sb.test", anonKey: "anon" };

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function jwt(claims: Record<string, unknown>): string {
  const part = btoa(JSON.stringify(claims)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return `h.${part}.s`;
}

function setup(responses: (Response | Error)[]) {
  const store = memoryStore();
  const calls: { url: string; body: unknown }[] = [];
  let now = 1_000_000;
  const auth = new Auth({
    store,
    config: async () => CONFIG,
    fetcher: async (url, init) => {
      calls.push({ url, body: JSON.parse(String(init?.body)) });
      const next = responses.shift() ?? json(500, {});
      if (next instanceof Error) throw next;
      return next;
    },
    now: () => now,
  });
  return { auth, store, calls, tick: (ms: number) => (now += ms) };
}

describe("Auth", () => {
  it("signs in with a password and keeps the tokens in the session store", async () => {
    const { auth, store, calls } = setup([json(200, { access_token: "a1", refresh_token: "r1", expires_in: 3600 })]);
    await auth.signInWithPassword(" mia@example.com ", "pw");
    expect(calls[0]?.url).toBe("https://sb.test/auth/v1/token?grant_type=password");
    expect(await auth.getToken()).toBe("a1");
    expect(store.data.get(AUTH_KEY)).toMatchObject({ access_token: "a1", email: "mia@example.com" });
  });

  it("shows the Supabase error", async () => {
    const { auth } = setup([json(400, { error_description: "Invalid login credentials" })]);
    await expect(auth.signInWithPassword("a@b.c", "x")).rejects.toThrow("Invalid login credentials");
    expect(auth.current).toBeNull();
  });

  it("refreshes a token that is about to expire, once for calls that overlap", async () => {
    const { auth, calls, tick } = setup([
      json(200, { access_token: "a1", refresh_token: "r1", expires_in: 3600 }),
      json(200, { access_token: "a2", refresh_token: "r2", expires_in: 3600 }),
    ]);
    await auth.signInWithPassword("a@b.c", "x");
    tick(3600_000 - REFRESH_MARGIN_MS + 1);
    const [one, two] = await Promise.all([auth.getToken(), auth.getToken()]);
    expect([one, two]).toEqual(["a2", "a2"]);
    expect(calls).toHaveLength(2);
    expect(calls[1]?.body).toEqual({ refresh_token: "r1" });
  });

  it("signs out when the refresh fails", async () => {
    const { auth, store } = setup([
      json(200, { access_token: "a1", refresh_token: "r1", expires_in: 3600 }),
      json(400, { msg: "bad" }),
    ]);
    await auth.signInWithPassword("a@b.c", "x");
    expect(await auth.refresh()).toBeNull();
    expect(auth.current).toBeNull();
    expect(store.data.has(AUTH_KEY)).toBe(false);
  });

  it("keeps the session when the sign-in service does not answer", async () => {
    const { auth, store, tick } = setup([
      json(200, { access_token: "a1", refresh_token: "r1", expires_in: 3600 }),
      new TypeError("Failed to fetch"),
      new TypeError("Failed to fetch"),
    ]);
    await auth.signInWithPassword("a@b.c", "x");
    tick(3600_000 - REFRESH_MARGIN_MS + 1);
    expect(await auth.getToken()).toBe("a1");
    expect(store.data.has(AUTH_KEY)).toBe(true);
    tick(REFRESH_MARGIN_MS);
    expect(await auth.getToken()).toBeNull();
    expect(auth.current).not.toBeNull();
  });

  it("lets a sign-out win over an older password sign-in", async () => {
    let answer: (response: Response) => void = () => undefined;
    const auth = new Auth({
      store: memoryStore(),
      config: async () => CONFIG,
      fetcher: () => new Promise<Response>((resolve) => (answer = resolve)),
    });
    const late = auth.signInWithPassword("a@b.c", "x");
    await Promise.resolve();
    await auth.signOut();
    answer(json(200, { access_token: "a1", refresh_token: "r1", expires_in: 3600 }));
    await expect(late).rejects.toThrow("newer sign-in");
    expect(auth.current).toBeNull();
  });

  it("says when the sign-in service does not answer", async () => {
    const { auth } = setup([new TypeError("Failed to fetch")]);
    await expect(auth.signInWithPassword("a@b.c", "x")).rejects.toThrow("could not reach the sign-in service");
  });

  it("uses a pasted token until its exp claim", async () => {
    const { auth, tick } = setup([]);
    await auth.useAccessToken(jwt({ exp: 1_000 + 600 }));
    expect(await auth.getToken()).not.toBeNull();
    tick(600_000);
    expect(await auth.getToken()).toBeNull();
  });

  it("loads the saved tokens", async () => {
    const { auth, store } = setup([]);
    await store.set(AUTH_KEY, { access_token: "x", refresh_token: null, expires_at: 5, email: null });
    expect(await auth.load()).toMatchObject({ access_token: "x" });
    await store.set(AUTH_KEY, { nope: true });
    expect(await auth.load()).toBeNull();
  });

  it("reads the expiry of a JWT", () => {
    expect(jwtExpiresIn(jwt({ exp: 2_000 }), 1_000_000)).toBe(1_000);
    expect(jwtExpiresIn("opaque", 0)).toBe(3600);
  });
});
