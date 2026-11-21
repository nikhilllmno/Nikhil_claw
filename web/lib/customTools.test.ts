import { describe, expect, it, vi } from "vitest";
import { ApiClient, type CustomTool, type CustomToolVersion, type TokenSource } from "./api";
import {
  createBody,
  credentialOf,
  definitionOf,
  draftOf,
  emptyDraft,
  needsApproval,
  parseHeaders,
  testArguments,
  toolState,
} from "./customTools";

describe("custom tools", () => {
  it("turns an http draft into a definition", () => {
    const draft = {
      ...emptyDraft("http"),
      name: "weather",
      description: "Get the weather.",
      method: "POST" as const,
      headers: "X-Trace: {{name}}\n\nAccept: application/json",
      body: '{"who": "{{name}}"}',
      authType: "api_key" as const,
      authWhere: "query" as const,
      authName: "key",
      responsePath: "data.items",
      responseFields: "id, name ,",
      secret: "k-1",
    };
    const body = createBody(draft, true);
    expect(typeof body).toBe("object");
    if (typeof body === "string") return;
    expect(body.http).toEqual({
      method: "POST",
      url: "https://api.example.com/greet?name={{name}}",
      headers: { "X-Trace": "{{name}}", Accept: "application/json" },
      body: { who: "{{name}}" },
      auth: { type: "api_key", query: "key" },
      response: {
        path: "data.items",
        fields: ["id", "name"],
        max_chars: 20000,
      },
    });
    expect(body.credential).toEqual({ value: "k-1" });
    // A member who cannot set the credential sends none.
    expect((createBody(draft, false) as { credential: unknown }).credential).toBeNull();
  });

  it("turns a script draft into a definition", () => {
    const draft = {
      ...emptyDraft("script"),
      description: "Greet.",
      language: "node" as const,
      timeout: "10",
    };
    const definition = definitionOf(draft);
    expect(definition).toMatchObject({
      kind: "script",
      script: { language: "node", timeout_s: 10 },
    });
    expect(credentialOf(draft)).toBeNull();
  });

  it("finds the problems of a draft", () => {
    const good = { ...emptyDraft("http"), name: "ok_name", description: "x" };
    expect(createBody({ ...good, name: "Bad" }, true)).toMatch(/name/);
    expect(definitionOf({ ...good, description: " " })).toMatch(/description/);
    expect(definitionOf({ ...good, schema: "{" })).toMatch(/not valid JSON/);
    expect(definitionOf({ ...good, schema: '{"type": "array"}' })).toMatch(/type/);
    expect(definitionOf({ ...good, url: "http://x.test" })).toMatch(/https/);
    expect(definitionOf({ ...good, body: "{}" })).toMatch(/no body/);
    expect(definitionOf({ ...good, headers: "no colon" })).toMatch(/Name: value/);
    expect(
      definitionOf({
        ...emptyDraft("script"),
        description: "x",
        timeout: "500",
      }),
    ).toMatch(/time limit/);
    expect(parseHeaders("A: 1:2")).toEqual({ A: "1:2" });
  });

  it("reads a version back into a draft", () => {
    const version = {
      version: 2,
      kind: "http",
      description: "d",
      policy: "allow",
      input_schema: { type: "object" },
      http: {
        method: "GET",
        url: "https://a.test/x",
        headers: { A: "1" },
        body: null,
        auth: { type: "api_key", header: "X-K", query: null },
        response: { path: null, fields: ["id"], max_chars: 500 },
      },
    } as unknown as CustomToolVersion;
    const tool = { name: "a_tool", scope: "workspace" } as CustomTool;
    const draft = draftOf(tool, version);
    expect(draft).toMatchObject({
      name: "a_tool",
      headers: "A: 1",
      authWhere: "header",
      authName: "X-K",
      responseFields: "id",
    });
    expect(definitionOf(draft)).toMatchObject({
      http: {
        auth: { type: "api_key", header: "X-K" },
        response: { max_chars: 500 },
      },
    });
  });

  it("parses the test arguments", () => {
    expect(testArguments("")).toEqual({});
    expect(testArguments('{"a": 1}')).toEqual({ a: 1 });
    expect(testArguments("[1]")).toMatch(/object/);
    expect(testArguments("{")).toMatch(/JSON/);
  });

  it("describes the state of a tool", () => {
    const tool = {
      current_version: null,
      pending: 1,
      current: null,
    } as unknown as CustomTool;
    expect(toolState(tool)).toBe("Waiting for an admin");
    const live = {
      current_version: 3,
      pending: 2,
      current: { policy: "ask" },
    } as unknown as CustomTool;
    expect(toolState(live)).toBe("Version 3 · Ask me before each call · 2 change(s) wait for an admin");
    expect(needsApproval("user", false)).toBe(false);
    expect(needsApproval("workspace", false)).toBe(true);
    expect(needsApproval("org", true)).toBe(false);
  });
});

describe("ApiClient custom tools", () => {
  const tokens: TokenSource = {
    generation: 1,
    getToken: async () => "t",
    refresh: async () => "t",
    signOut: () => undefined,
  };

  it("calls the custom tool routes", async () => {
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      void url;
      if (init?.method === "DELETE") return new Response(null, { status: 204 });
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    const client = new ApiClient("https://api.test", tokens, fetcher);
    await client.customTools("w1");
    await client.customToolVersions("t1");
    await client.revertCustomTool("t1", 2);
    await client.reviewCustomTool("t1", 3, "approve");
    await client.setCustomToolCredential("t1", { value: "k" });
    await client.testCustomTool("t1", { a: 1 });
    await expect(client.deleteCustomTool("t1")).resolves.toBeNull();
    const calls = fetcher.mock.calls.map(([url, init]) => [init?.method, url, init?.body]);
    expect(calls).toEqual([
      ["GET", "https://api.test/workspaces/w1/custom-tools", undefined],
      ["GET", "https://api.test/custom-tools/t1/versions", undefined],
      ["POST", "https://api.test/custom-tools/t1/revert", '{"version":2}'],
      ["POST", "https://api.test/custom-tools/t1/versions/3/approve", undefined],
      ["PUT", "https://api.test/custom-tools/t1/credential", '{"value":"k"}'],
      ["POST", "https://api.test/custom-tools/t1/test", '{"arguments":{"a":1},"version":null}'],
      ["DELETE", "https://api.test/custom-tools/t1", undefined],
    ]);
  });
});
