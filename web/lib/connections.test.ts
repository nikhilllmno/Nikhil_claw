import { describe, expect, it, vi } from "vitest";
import { ApiClient, type Connection, type Connector, type TokenSource } from "./api";
import {
  connectionOf,
  formProblem,
  formValues,
  initialValues,
  isSafeRedirect,
  listFor,
  parseConnectionQuery,
  stateLabel,
  stateTone,
  toolSummary,
  withConnection,
  withoutConnection,
  withoutEntryParams,
} from "./connections";

const WS = "11111111-2222-4333-8444-555555555555";
const CONN = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

const NOTION: Connector = {
  name: "notion",
  title: "Notion",
  description: "Pages",
  kind: "http_api",
  scopes: ["personal", "workspace"],
  fields: [
    { name: "token", label: "Secret", secret: true, required: true, help: "" },
    { name: "space", label: "Space", secret: false, required: false, help: "" },
  ],
  tools: [
    { name: "notion_search", description: "", access: "read" },
    { name: "notion_page", description: "", access: "read" },
    { name: "notion_add", description: "", access: "write" },
  ],
};

function connection(overrides: Partial<Connection> = {}): Connection {
  return {
    id: CONN,
    workspace_id: WS,
    connector: "notion",
    kind: "http_api",
    scope: "personal",
    owner_user_id: "u1",
    state: "pending",
    last_error: null,
    last_health_at: null,
    created_at: "2026-11-20T00:00:00Z",
    updated_at: "2026-11-20T00:00:00Z",
    fields: { space: "team" },
    has_secret: true,
    ...overrides,
  };
}

describe("parseConnectionQuery", () => {
  it("reads a connect link with a default personal scope", () => {
    expect(parseConnectionQuery(`?workspace=${WS}&connect=gmail`)).toEqual({
      kind: "connect",
      workspaceId: WS,
      connector: "gmail",
      scope: "personal",
    });
    expect(parseConnectionQuery(`?workspace=${WS}&connect=gmail&scope=workspace`)).toMatchObject({
      scope: "workspace",
    });
  });

  it("reads the callback of the sign-in", () => {
    expect(parseConnectionQuery(`?workspace=${WS}&connection=${CONN}`)).toEqual({
      kind: "callback",
      workspaceId: WS,
      connectionId: CONN,
    });
  });

  it("refuses bad values", () => {
    expect(parseConnectionQuery("")).toBeNull();
    expect(parseConnectionQuery("?connect=gmail")).toBeNull();
    expect(parseConnectionQuery(`?workspace=nope&connect=gmail`)).toBeNull();
    expect(parseConnectionQuery(`?workspace=${WS}&connect=../x`)).toBeNull();
    expect(parseConnectionQuery(`?workspace=${WS}&connect=Gmail`)).toBeNull();
    expect(parseConnectionQuery(`?workspace=${WS}&connect=gmail&scope=org`)).toBeNull();
    expect(parseConnectionQuery(`?workspace=${WS}&connection=1`)).toBeNull();
  });
});

describe("isSafeRedirect", () => {
  it("allows only https", () => {
    expect(isSafeRedirect("https://connect.composio.dev/link/x")).toBe(true);
    expect(isSafeRedirect("http://connect.composio.dev/link/x")).toBe(false);
    expect(isSafeRedirect("javascript:alert(1)")).toBe(false);
    expect(isSafeRedirect("not a url")).toBe(false);
    expect(isSafeRedirect(null)).toBe(false);
    expect(isSafeRedirect("https://connect.composio.dev@evil.test/")).toBe(false);
    expect(isSafeRedirect("https://user:pw@connect.composio.dev/")).toBe(false);
  });
});

describe("states", () => {
  it("has a label and a tone for each state", () => {
    expect(stateLabel("connected")).toBe("Connected");
    expect(stateLabel("initiated")).toBe("Waiting for sign-in");
    expect(stateLabel("other")).toBe("other");
    expect(stateTone("connected")).toBe("ok");
    expect(stateTone("pending")).toBe("wait");
    expect(stateTone("disabled")).toBe("off");
    expect(stateTone("expired")).toBe("error");
  });
});

describe("forms", () => {
  it("needs the required fields", () => {
    expect(formProblem(NOTION.fields, {})).toBe("Fill in Secret.");
    expect(formProblem(NOTION.fields, { token: "  " })).toBe("Fill in Secret.");
    expect(formProblem(NOTION.fields, { token: "s" })).toBeNull();
  });

  it("sends only the trimmed fields of the connector", () => {
    expect(formValues(NOTION.fields, { token: " s ", space: "", other: "x" })).toEqual({ token: "s" });
  });

  it("starts with the saved fields and an empty secret", () => {
    expect(initialValues(NOTION, connection())).toEqual({ token: "", space: "team" });
    expect(initialValues(NOTION, null)).toEqual({ token: "", space: "" });
  });
});

describe("helpers", () => {
  it("finds the connection of a connector and a scope", () => {
    const list = [connection(), connection({ id: "w", scope: "workspace", owner_user_id: null })];
    expect(connectionOf(list, "notion", "workspace")?.id).toBe("w");
    expect(connectionOf(list, "gmail", "personal")).toBeNull();
  });

  it("sums up the tools", () => {
    expect(toolSummary(NOTION)).toBe("2 read, 1 write (asks first)");
    expect(toolSummary({ ...NOTION, tools: [] })).toBe("0 read");
  });

  it("removes the entry parameters from the address", () => {
    expect(withoutEntryParams(`?workspace=${WS}&connection=${CONN}`)).toBe("");
    expect(withoutEntryParams(`?workspace=${WS}&connect=gmail&x=1`)).toBe("?x=1");
  });
});

describe("ApiClient connections", () => {
  const tokens: TokenSource = {
    generation: 1,
    getToken: async () => "t",
    refresh: async () => "t",
    signOut: () => undefined,
  };

  it("calls the connection routes", async () => {
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      void url;
      if (init?.method === "DELETE") return new Response(null, { status: 204 });
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    const client = new ApiClient("https://api.test", tokens, fetcher);
    await client.listConnectors();
    await client.listConnections(WS);
    await client.startConnection(WS, "gmail", "workspace");
    await client.checkConnection(CONN);
    await client.submitConnectionForm(CONN, { token: "s" });
    await client.disableConnection(CONN);
    await expect(client.deleteConnection(CONN)).resolves.toBeNull();
    const calls = fetcher.mock.calls.map(([url, init]) => [init?.method, url, init?.body]);
    expect(calls).toEqual([
      ["GET", "https://api.test/connectors", undefined],
      ["GET", `https://api.test/workspaces/${WS}/connections`, undefined],
      [
        "POST",
        `https://api.test/workspaces/${WS}/connections`,
        '{"connector":"gmail","scope":"workspace"}',
      ],
      ["POST", `https://api.test/connections/${CONN}/check`, undefined],
      ["PUT", `https://api.test/connections/${CONN}/form`, '{"values":{"token":"s"}}'],
      ["POST", `https://api.test/connections/${CONN}/disable`, undefined],
      ["DELETE", `https://api.test/connections/${CONN}`, undefined],
    ]);
  });
});

describe("the connection list of a workspace", () => {
  const OTHER_WS = "99999999-2222-4333-8444-555555555555";

  it("shows the rows only for their workspace", () => {
    const list = { workspaceId: WS, items: [connection()] };
    expect(listFor(list, WS)).toEqual([connection()]);
    expect(listFor(list, OTHER_WS)).toBeNull();
    expect(listFor(null, WS)).toBeNull();
  });

  it("puts a saved connection only into the list of its workspace", () => {
    const list = { workspaceId: WS, items: [connection({ state: "pending" })] };
    const saved = withConnection(list, connection({ state: "connected" }));
    expect(saved?.items.map((c) => c.state)).toEqual(["connected"]);
    // An old answer for another workspace does not change the list.
    const other = { workspaceId: OTHER_WS, items: [] };
    expect(withConnection(other, connection())).toBe(other);
    expect(withConnection(null, connection())).toBeNull();
  });

  it("removes a connection", () => {
    const list = { workspaceId: WS, items: [connection()] };
    expect(withoutConnection(list, CONN)?.items).toEqual([]);
    expect(withoutConnection(null, CONN)).toBeNull();
  });
});
