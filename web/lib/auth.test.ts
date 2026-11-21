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
