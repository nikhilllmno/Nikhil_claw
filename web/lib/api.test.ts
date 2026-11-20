import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, type ChatMessage, errorMessage, errorText, type TokenSource } from "./api";
import { AuthSession } from "./auth";
import type { KeyValueStore } from "./pkce";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function tokens(overrides: Partial<TokenSource> = {}): TokenSource & {
  refresh: ReturnType<typeof vi.fn>;
  signOut: ReturnType<typeof vi.fn>;
} {
  return {
    generation: 1,
    getToken: vi.fn(async () => "old"),
    refresh: vi.fn(async () => "new"),
    signOut: vi.fn(),
    ...overrides,
  } as never;
}

function authOf(init: RequestInit | undefined): string {
  return (init?.headers as Record<string, string>).Authorization ?? "";
}

describe("ApiClient", () => {
  it("sends the Bearer token and the JSON body", async () => {
    const fetcher = vi.fn(async () => json(200, { ok: true }));
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    await client.json("/sessions", { method: "POST", body: { a: 1 } });
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.test/sessions");
    expect(init.method).toBe("POST");
    expect(authOf(init)).toBe("Bearer old");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(init.body).toBe('{"a":1}');
  });

  it("refreshes once on 401 and tries again with the new token", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(json(401, { detail: "expired" }))
      .mockResolvedValueOnce(json(200, { orgs: [] }));
    const source = tokens();
    const client = new ApiClient("https://api.test", source, fetcher);
    await expect(client.me()).resolves.toEqual({ orgs: [] });
    expect(source.refresh).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(authOf(fetcher.mock.calls[0]?.[1])).toBe("Bearer old");
    expect(authOf(fetcher.mock.calls[1]?.[1])).toBe("Bearer new");
    expect(source.signOut).not.toHaveBeenCalled();
  });

  it("signs out when the refresh fails", async () => {
    const fetcher = vi.fn(async () => json(401, {}));
    const source = tokens({ refresh: vi.fn(async () => null) });
    const client = new ApiClient("https://api.test", source, fetcher);
    await expect(client.me()).rejects.toMatchObject({ status: 401 });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(source.signOut).toHaveBeenCalledTimes(1);
  });

  it("signs out and does not refresh again when the retry is also 401", async () => {
    const fetcher = vi.fn(async () => json(401, {}));
    const source = tokens();
    const client = new ApiClient("https://api.test", source, fetcher);
    await expect(client.me()).rejects.toBeInstanceOf(ApiError);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(source.refresh).toHaveBeenCalledTimes(1);
    expect(source.signOut).toHaveBeenCalledTimes(1);
  });

  it("does not refresh on other errors and shows the detail text", async () => {
    const fetcher = vi.fn(async () => json(422, { detail: "the soul contains a secret" }));
    const source = tokens();
    const client = new ApiClient("https://api.test", source, fetcher);
    await expect(client.updateAgentProfile("o", "p", { soul: "x" })).rejects.toMatchObject({
      status: 422,
      message: "the soul contains a secret",
    });
    expect(source.refresh).not.toHaveBeenCalled();
  });

  it("returns null for 204", async () => {
    const fetcher = vi.fn(async () => new Response(null, { status: 204 }));
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    await expect(client.answerQuestion("s", "r/1", ["a"])).resolves.toBeNull();
    expect((fetcher.mock.calls[0] as unknown[] | undefined)?.[0]).toBe("https://api.test/sessions/s/questions/r%2F1/answer");
  });
});

function memory(): KeyValueStore {
  const data = new Map<string, string>();
  return {
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  };
}

/** A promise that the test settles later. */
function later<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

describe("ApiClient across an account change", () => {
  /** An `AuthSession` that signs in as account A, with a refresh token if `refreshable`. */
  async function signedInAsA(authFetch: ReturnType<typeof vi.fn>, refreshable = false) {
    const auth = new AuthSession({
      config: { url: "https://x.supabase.co", anonKey: "anon" },
      session: memory(),
      local: memory(),
      fetcher: authFetch as never,
    });
    if (refreshable) await auth.signInWithPassword("a@b.c", "pw");
    else auth.useAccessToken("token-a");
    return auth;
  }
  const accountSwitch = (auth: AuthSession) => {
    auth.signOut();
    auth.useAccessToken("token-b");
  };

  it("drops the 401 of the old account: no refresh, no retry, the new account stays signed in", async () => {
    const authFetch = vi.fn();
    const auth = await signedInAsA(authFetch);
    const answer = later<Response>();
    const fetcher = vi.fn(() => answer.promise);
    const client = new ApiClient("https://api.test", auth, fetcher);
    const call = client.me();
    const rejected = expect(call).rejects.toBeInstanceOf(ApiError);
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    accountSwitch(auth);
    answer.resolve(json(401, {}));
    await rejected;
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(authFetch).not.toHaveBeenCalled();
    expect(auth.current?.access_token).toBe("token-b");
  });

  it("drops a refresh of the old account that ends after the switch", async () => {
    const refreshAnswer = later<Response>();
    const authFetch = vi
      .fn()
      .mockResolvedValueOnce(json(200, { access_token: "a1", refresh_token: "r1", expires_in: 3600 }))
      .mockReturnValueOnce(refreshAnswer.promise);
    const auth = await signedInAsA(authFetch, true);
    const fetcher = vi.fn(async () => json(401, {}));
    const client = new ApiClient("https://api.test", auth, fetcher);
    const rejected = expect(client.me()).rejects.toBeInstanceOf(ApiError);
    await vi.waitFor(() => expect(authFetch).toHaveBeenCalledTimes(2)); // the refresh runs
    accountSwitch(auth);
    refreshAnswer.resolve(json(200, { access_token: "a2", refresh_token: "r2", expires_in: 3600 }));
    await rejected;
    expect(fetcher).toHaveBeenCalledTimes(1); // no retry with the token of account A
    expect(auth.current?.access_token).toBe("token-b");
  });

  it("does not sign the new account out when the refresh of the old one fails late", async () => {
    const refreshAnswer = later<Response>();
    const authFetch = vi
      .fn()
      .mockResolvedValueOnce(json(200, { access_token: "a1", refresh_token: "r1", expires_in: 3600 }))
      .mockReturnValueOnce(refreshAnswer.promise);
    const auth = await signedInAsA(authFetch, true);
    const client = new ApiClient("https://api.test", auth, vi.fn(async () => json(401, {})));
    const rejected = expect(client.me()).rejects.toBeInstanceOf(ApiError);
    await vi.waitFor(() => expect(authFetch).toHaveBeenCalledTimes(2));
    accountSwitch(auth);
    refreshAnswer.resolve(json(400, { msg: "Invalid Refresh Token" }));
    await rejected;
    expect(auth.current?.access_token).toBe("token-b");
  });

  it("drops a successful answer for the old account", async () => {
    const authFetch = vi.fn();
    const auth = await signedInAsA(authFetch);
    const answer = later<Response>();
    const client = new ApiClient("https://api.test", auth, vi.fn(() => answer.promise));
    const rejected = expect(client.me()).rejects.toBeInstanceOf(ApiError);
    accountSwitch(auth);
    answer.resolve(json(200, { orgs: [] }));
    await rejected;
  });

  it("still signs out when the current account gets a 401 twice", async () => {
    const authFetch = vi.fn(async () => json(400, { msg: "no" }));
    const auth = await signedInAsA(authFetch);
    const client = new ApiClient("https://api.test", auth, vi.fn(async () => json(401, {})));
    await expect(client.me()).rejects.toMatchObject({ status: 401 });
    expect(auth.current).toBeNull();
  });
});

describe("listMessages", () => {
  const message = (seq: number): ChatMessage => ({
    id: `m${seq}`,
    seq,
    run_id: null,
    role: "user",
    author_user_id: null,
    text: String(seq),
    created_at: "2026-11-20T00:00:00Z",
  });

  it("reads every page with the after cursor", async () => {
    const all = Array.from({ length: 1203 }, (_, i) => message(i + 1));
    const fetcher = vi.fn(async (input: string) => {
      const query = new URL(input).searchParams;
      const after = Number(query.get("after"));
      const limit = Number(query.get("limit"));
      return json(200, all.filter((m) => m.seq > after).slice(0, limit));
    });
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    const loaded = await client.listMessages("s1");
    expect(loaded).toEqual(all);
    expect(fetcher.mock.calls.map(([url]) => new URL(url as string).searchParams.get("after"))).toEqual([
      "0",
      "500",
      "1000",
    ]);
  });

  it("makes one call for a short session", async () => {
    const fetcher = vi.fn(async () => json(200, [message(1), message(2)]));
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    await expect(client.listMessages("s1")).resolves.toHaveLength(2);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});

describe("errorText", () => {
  it("uses the message of an Error and the fallback for anything else", () => {
    expect(errorText(new Error("boom"), "fallback")).toBe("boom");
    expect(errorText("boom", "fallback")).toBe("fallback");
  });
});

describe("errorMessage", () => {
  it("joins the messages of a validation error", () => {
    expect(
      errorMessage(422, {
        detail: [
          { loc: ["body", "name"], msg: "String should have at least 1 character" },
          { loc: ["body"], msg: "Value error" },
        ],
      }),
    ).toBe("name: String should have at least 1 character; Value error");
  });

  it("has a text for a status without a body", () => {
    expect(errorMessage(500, null)).toBe("The request failed (500).");
    expect(errorMessage(403, {})).toBe("You do not have permission to do this.");
  });
});
