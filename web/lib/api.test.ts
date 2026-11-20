import { describe, expect, it, vi } from "vitest";
import {
  ApiClient,
  ApiError,
  attachmentName,
  type ChatMessage,
  errorMessage,
  errorText,
  query,
  type TokenSource,
} from "./api";
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

  it("drops an answer whose body is still downloading when the account changes", async () => {
    const auth = await signedInAsA(vi.fn());
    let finish!: () => void;
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('{"id":"s-a",'));
        finish = () => {
          controller.enqueue(encoder.encode('"title":"A"}'));
          controller.close();
        };
      },
    });
    const fetcher = vi.fn(async () => new Response(body, { status: 200 }));
    const client = new ApiClient("https://api.test", auth, fetcher);
    const rejected = expect(
      client.createSession({ audience: "owner" } as never),
    ).rejects.toMatchObject({ status: 401 });
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1)); // the headers are in
    await new Promise((resolve) => setTimeout(resolve, 0)); // the body read has started
    accountSwitch(auth);
    finish();
    await rejected;
    expect(auth.current?.access_token).toBe("token-b");
  });

  it("does not send the next page of a read that started as another account", async () => {
    const auth = await signedInAsA(vi.fn());
    const page = Array.from({ length: 500 }, (_, i) => ({ seq: i + 1 }));
    const fetcher = vi.fn(async () => {
      accountSwitch(auth); // the user switches after the first page is in
      return json(200, page);
    });
    const client = new ApiClient("https://api.test", auth, fetcher);
    await expect(client.listMessages("s1")).rejects.toMatchObject({ status: 401 });
    expect(fetcher).toHaveBeenCalledTimes(1); // page 2 is not sent with the token of B
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

describe("job calls", () => {
  const ORG = "org-1";
  const JOB = "job-1";

  function call(fetcher: ReturnType<typeof vi.fn>): { url: string; init: RequestInit } {
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    return { url, init };
  }

  async function run(
    status: number,
    body: unknown,
    invoke: (client: ApiClient) => Promise<unknown>,
  ): Promise<{ url: string; init: RequestInit; result: unknown }> {
    const fetcher = vi.fn(async () => json(status, body));
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    const result = await invoke(client);
    return { ...call(fetcher), result };
  }

  it("previews a schedule with and without a time zone", async () => {
    const preview = { schedule: { kind: "interval", every_s: 7200 }, text: "every 2 hours", next_fires: [] };
    const withTz = await run(200, preview, (c) => c.previewSchedule("every 2 hours", "Asia/Kolkata"));
    expect(withTz.url).toBe("https://api.test/jobs/preview");
    expect(withTz.init.method).toBe("POST");
    expect(withTz.init.body).toBe('{"schedule":"every 2 hours","tz":"Asia/Kolkata"}');
    expect(withTz.result).toEqual(preview);
    const without = await run(200, preview, (c) => c.previewSchedule("hourly"));
    expect(without.init.body).toBe('{"schedule":"hourly"}');
  });

  it("shows the detail text of a bad schedule", async () => {
    const fetcher = vi.fn(async () => json(400, { detail: "the unit 'foo' is not known" }));
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    await expect(client.previewSchedule("every 2 foo")).rejects.toMatchObject({
      status: 400,
      message: "the unit 'foo' is not known",
    });
  });

  it("lists and reads jobs", async () => {
    const list = await run(200, [], (c) => c.listJobs(ORG));
    expect(list.url).toBe(`https://api.test/orgs/${ORG}/jobs`);
    expect(list.init.method).toBe("GET");
    const one = await run(200, { id: JOB }, (c) => c.getJob(ORG, JOB));
    expect(one.url).toBe(`https://api.test/orgs/${ORG}/jobs/${JOB}`);
    expect(one.result).toEqual({ id: JOB });
  });

  it("creates a job with a POST and updates it with a PATCH", async () => {
    const created = await run(201, { id: JOB }, (c) =>
      c.createJob(ORG, { name: "Digest", schedule: "daily at 9am", prompt: "Send the digest." }),
    );
    expect(created.url).toBe(`https://api.test/orgs/${ORG}/jobs`);
    expect(created.init.method).toBe("POST");
    expect(created.init.body).toBe('{"name":"Digest","schedule":"daily at 9am","prompt":"Send the digest."}');
    expect(created.result).toEqual({ id: JOB });
    const updated = await run(200, { id: JOB }, (c) => c.updateJob(ORG, JOB, { model: "m" }));
    expect(updated.url).toBe(`https://api.test/orgs/${ORG}/jobs/${JOB}`);
    expect(updated.init.method).toBe("PATCH");
    expect(updated.init.body).toBe('{"model":"m"}');
  });

  it("approves one version", async () => {
    const { url, init } = await run(200, { id: JOB }, (c) => c.approveJob(ORG, JOB, 3));
    expect(url).toBe(`https://api.test/orgs/${ORG}/jobs/${JOB}/approve`);
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"version":3}');
  });

  it("sends the actions pause, resume, run, and cancel as POST without a body", async () => {
    const cases: [string, (c: ApiClient) => Promise<unknown>, unknown][] = [
      ["pause", (c) => c.pauseJob(ORG, JOB), { id: JOB }],
      ["resume", (c) => c.resumeJob(ORG, JOB), { id: JOB }],
      ["run", (c) => c.runJob(ORG, JOB), { fire_id: "f" }],
      ["cancel", (c) => c.cancelJob(ORG, JOB), { cancelled: 1 }],
    ];
    for (const [action, invoke, body] of cases) {
      const { url, init, result } = await run(200, body, invoke);
      expect(url).toBe(`https://api.test/orgs/${ORG}/jobs/${JOB}/${action}`);
      expect(init.method).toBe("POST");
      expect(init.body).toBeUndefined();
      expect(result).toEqual(body);
    }
  });

  it("removes a job with a DELETE and gets null for 204", async () => {
    const fetcher = vi.fn(async () => new Response(null, { status: 204 }));
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    await expect(client.deleteJob(ORG, JOB)).resolves.toBeNull();
    const { url, init } = call(fetcher);
    expect(url).toBe(`https://api.test/orgs/${ORG}/jobs/${JOB}`);
    expect(init.method).toBe("DELETE");
    expect(authOf(init)).toBe("Bearer old");
  });

  it("shows the answer of a job that is not found or off", async () => {
    const missing = new ApiClient("https://api.test", tokens(), vi.fn(async () => json(404, { detail: "the job does not exist" })));
    await expect(missing.getJob(ORG, JOB)).rejects.toMatchObject({ status: 404, message: "the job does not exist" });
    const off = new ApiClient("https://api.test", tokens(), vi.fn(async () => json(409, { detail: "scheduled jobs are off in this process" })));
    await expect(off.listJobs(ORG)).rejects.toMatchObject({ status: 409 });
  });
});

describe("query", () => {
  it("leaves out null values and encodes the rest", () => {
    expect(query({ q: "a b&c", workspace_id: null, limit: 10 })).toBe("?q=a+b%26c&limit=10");
    expect(query({ scope: null })).toBe("");
  });
});

describe("attachmentName", () => {
  it("reads the file name of the header and refuses a path", () => {
    expect(attachmentName('attachment; filename="session-1.md"', "x.md")).toBe("session-1.md");
    expect(attachmentName(null, "x.md")).toBe("x.md");
    expect(attachmentName('attachment; filename="../etc/passwd"', "x.md")).toBe("x.md");
  });
});

function firstUrl(fetcher: { mock: { calls: unknown[][] } }): string {
  return String(fetcher.mock.calls[0]?.[0]);
}

describe("ApiClient M3.12 calls", () => {
  it("sends the search query and the workspace filter", async () => {
    const fetcher = vi.fn(async () => json(200, []));
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    await client.searchSessions("o1", "deploy plan", "w1", 5);
    expect(firstUrl(fetcher)).toBe(
      "https://api.test/orgs/o1/session-search?q=deploy+plan&workspace_id=w1&limit=5",
    );
  });

  it("puts the new memory entries with the profile and the reason", async () => {
    const fetcher = vi.fn(async () => json(200, { target: "agent", entries: ["a"] }));
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    await client.editMemory("w1", "agent", ["a"], "p1", "fix");
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.test/workspaces/w1/memory/agent");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({
      entries: ["a"],
      agent_profile_id: "p1",
      reason: "fix",
    });
  });

  it("encodes the skill name and the scope", async () => {
    const fetcher = vi.fn(async () => json(200, {}));
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    await client.getSkill("w1", "a b", "user");
    expect(firstUrl(fetcher)).toBe("https://api.test/workspaces/w1/skills/a%20b?scope=user");
  });

  it("returns the export file with the name of the header", async () => {
    const fetcher = vi.fn(
      async () =>
        new Response("# Session", {
          status: 200,
          headers: { "Content-Disposition": 'attachment; filename="session-abc.md"' },
        }),
    );
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    const file = await client.exportSession("abcdef0123", "markdown");
    expect(firstUrl(fetcher)).toBe("https://api.test/sessions/abcdef0123/export?format=markdown");
    expect(file.name).toBe("session-abc.md");
    expect(await file.blob.text()).toBe("# Session");
  });

  it("throws an ApiError when the export fails", async () => {
    const fetcher = vi.fn(async () => json(404, { detail: "the session does not exist" }));
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    await expect(client.exportSession("s1", "json")).rejects.toMatchObject({ status: 404 });
  });

  it("drops an export that ends after the account changed", async () => {
    const source = tokens();
    const fetcher = vi.fn(async () => {
      return new Response("{}", { status: 200 });
    });
    const client = new ApiClient("https://api.test", source, fetcher);
    const pending = client.exportSession("s1", "json");
    (source as { generation: number }).generation = 2;
    await expect(pending).rejects.toBeInstanceOf(ApiError);
  });
});
