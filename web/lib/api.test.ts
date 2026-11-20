import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, errorMessage, type TokenSource } from "./api";

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
