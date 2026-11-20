import { describe, expect, it, vi } from "vitest";
import { ApiClient, type Connection, type Connector, type TokenSource } from "./api";
import {
  EMPTY_API,
  EMPTY_SERVER,
  EMPTY_STDIO,
  apiBody,
  apiFields,
  apiProblem,
  canCheck,
  connectionOf,
  connectorFor,
  customServers,
  formProblem,
  formValues,
  hostedFields,
  hostedWhat,
  hasOAuthAnswer,
  initialValues,
  isSafeRedirect,
  listFor,
  mcpFields,
  mcpToolSummary,
  parseConnectionQuery,
  parseEndpoints,
  parsePatterns,
  patternProblem,
  serverBody,
  serverProblem,
  stateLabel,
  stateTone,
  stdioBody,
  stdioProblem,
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

describe("remote MCP servers", () => {
  const STATE = `${CONN}.abcdefghijklmnopqrstuvwx`;

  it("reads the OAuth answer without a workspace", () => {
    expect(parseConnectionQuery(`?code=c1&state=${STATE}`)).toEqual({
      kind: "oauth",
      state: STATE,
      code: "c1",
      error: null,
    });
    expect(parseConnectionQuery(`?error=access_denied&state=${STATE}`)).toEqual({
      kind: "oauth",
      state: STATE,
      code: null,
      error: "access_denied",
    });
  });

  it("refuses a bad OAuth answer", () => {
    expect(parseConnectionQuery(`?code=c1&state=short`)).toBeNull();
    expect(parseConnectionQuery(`?state=${STATE}`)).toBeNull();
    expect(parseConnectionQuery(`?code=c1&error=x&state=${STATE}`)).toBeNull();
    expect(parseConnectionQuery(`?code=${"c".repeat(4001)}&state=${STATE}`)).toBeNull();
  });

  it("removes the code and the state from the address", () => {
    expect(withoutEntryParams(`?code=c1&state=${STATE}&iss=x&tab=2`)).toBe("?tab=2");
    // A broken answer is still an answer: the page removes it and says so.
    expect(hasOAuthAnswer("?state=short&code=c1")).toBe(true);
    expect(parseConnectionQuery("?state=short&code=c1")).toBeNull();
    expect(hasOAuthAnswer("?error=access_denied")).toBe(true);
    expect(hasOAuthAnswer(`?workspace=${WS}&connection=${CONN}`)).toBe(false);
  });

  it("checks a new server before it goes to the API", () => {
    const good = { ...EMPTY_SERVER, name: " Docs ", url: "https://mcp.example.com/mcp" };
    expect(serverProblem(good)).toBeNull();
    expect(serverProblem({ ...good, name: "9docs" })).toMatch(/name/);
    expect(serverProblem({ ...good, url: "mcp.example.com" })).toMatch(/full address/);
    expect(serverProblem({ ...good, url: "http://mcp.example.com/mcp" })).toMatch(/https/);
    expect(serverProblem({ ...good, url: "https://u:p@mcp.example.com/" })).toMatch(/user name/);
    expect(serverProblem({ ...good, url: "https://mcp.example.com/?key=1" })).toMatch(/query/);
    expect(serverProblem({ ...good, auth: "header", header: "" })).toMatch(/header/);
    expect(serverProblem({ ...good, auth: "header", header: "Host" })).toMatch(/sets the Host/);
    expect(serverProblem({ ...good, auth: "header", header: "X-Api-Key" })).toBeNull();
    expect(serverBody({ ...good, oauthScope: " read ", header: "X" })).toEqual({
      name: "docs",
      url: "https://mcp.example.com/mcp",
      auth: "oauth",
      header: null,
      oauth_scope: "read",
      title: null,
      scope: "workspace",
    });
  });

  it("makes a card for each server that an admin added by URL", () => {
    const mcp = {
      custom: { title: "Docs", url: "https://mcp.example.com/mcp", auth: "bearer" as const, header: null },
      include: [],
      exclude: [],
      discovered_at: null,
      tools: [],
    };
    const rows = [
      connection({ connector: "docs", kind: "mcp_remote", scope: "workspace", mcp }),
      connection({ id: "x", connector: "docs", kind: "mcp_remote", scope: "personal", mcp }),
      connection({ connector: "notion" }),
    ];
    const cards = customServers(rows, [NOTION]);
    expect(cards.map((c) => [c.name, c.title, c.scopes])).toEqual([["docs", "Docs", ["workspace", "personal"]]]);
    expect(cards[0]?.fields.map((f) => [f.name, f.secret])).toEqual([["token", true]]);
    expect(mcpFields("oauth", null)).toEqual([]);
    expect(mcpFields("header", "X-Key")[0]?.help).toContain("X-Key");
  });

  it("gives each connection of a server by URL its own form", () => {
    const base = { include: [], exclude: [], discovered_at: null, tools: [] };
    const team = connection({
      connector: "docs",
      kind: "mcp_remote",
      scope: "workspace",
      mcp: { ...base, custom: { title: "Docs", url: "https://a.example.com/mcp", auth: "bearer", header: null } },
    });
    const mine = connection({
      id: "x",
      connector: "docs",
      kind: "mcp_remote",
      scope: "personal",
      mcp: { ...base, custom: { title: "Docs", url: "https://b.example.com/mcp", auth: "header", header: "X-Key" } },
    });
    const [card] = customServers([team, mine], [NOTION]);
    expect(card?.description).toBe("Added by URL: https://a.example.com/mcp, https://b.example.com/mcp");
    if (card === undefined) throw new Error("no card");
    expect(connectorFor(card, team).fields[0]?.help).toContain("Authorization");
    expect(connectorFor(card, mine).fields[0]?.help).toContain("X-Key");
    expect(connectorFor(card, mine).mcp?.url).toBe("https://b.example.com/mcp");
    expect(connectorFor(NOTION, connection())).toBe(NOTION);
    expect(connectorFor(card, null)).toBe(card);
    // A pending server with a form waits for the form. An OAuth server can be checked.
    expect(canCheck(card, { ...team, state: "pending" })).toBe(false);
    expect(canCheck(card, { ...team, state: "connected" })).toBe(true);
    const oauth = connection({
      connector: "docs",
      kind: "mcp_remote",
      state: "pending",
      mcp: { ...base, custom: { title: "Docs", url: "https://a.example.com/mcp", auth: "oauth", header: null } },
    });
    expect(canCheck(card, oauth)).toBe(true);
    expect(canCheck(NOTION, connection({ state: "pending" }))).toBe(false);
  });

  it("sums up the tools and checks the filters", () => {
    const tool = (name: string, access: "read" | "write", enabled: boolean) => ({
      name: `mcp_docs_${name}`,
      remote_name: name,
      description: "",
      access,
      enabled,
    });
    const mcp = {
      custom: null,
      include: [],
      exclude: ["drop"],
      discovered_at: "2026-11-20T00:00:00Z",
      tools: [tool("read", "read", true), tool("add", "write", true), tool("drop", "write", false)],
    };
    expect(mcpToolSummary(connection({ kind: "mcp_remote", mcp }))).toBe("2 tools, 1 read only, 1 off");
    expect(mcpToolSummary(connection({ kind: "mcp_remote", mcp: { ...mcp, discovered_at: null } }))).toBeNull();
    expect(mcpToolSummary(connection())).toBeNull();
    expect(parsePatterns(" list_*\n, drop ,\n")).toEqual(["list_*", "drop"]);
    expect(patternProblem(["list_*", "a/b:c"])).toBeNull();
    expect(patternProblem(["a b"])).toMatch(/not a pattern/);
    expect(patternProblem(Array.from({ length: 51 }, (_, i) => `t${i}`))).toMatch(/50/);
  });

  it("calls the MCP routes", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    await client.addMcpServer(WS, { name: "docs", url: "https://m.test/mcp", auth: "none", scope: "workspace" });
    await client.finishOAuth(STATE, "c1", null);
    await client.setConnectionTools(CONN, ["a*"], []);
    const calls = fetcher.mock.calls.map((call) => {
      const [url, init] = call as unknown as [string, RequestInit | undefined];
      return [init?.method, url, init?.body];
    });
    expect(calls).toEqual([
      [
        "POST",
        `https://api.test/workspaces/${WS}/connections/mcp`,
        '{"name":"docs","url":"https://m.test/mcp","auth":"none","scope":"workspace"}',
      ],
      ["POST", "https://api.test/connections/oauth/callback", `{"state":"${STATE}","code":"c1","error":null}`],
      ["PUT", `https://api.test/connections/${CONN}/tools`, '{"include":["a*"],"exclude":[]}'],
    ]);
  });
});

describe("stdio MCP servers (M4.11)", () => {
  const good = { ...EMPTY_STDIO, name: "Everything", packageName: "@modelcontextprotocol/server-everything", version: "2025.9.25" };

  it("checks the form", () => {
    expect(stdioProblem(good)).toBeNull();
    expect(stdioProblem({ ...good, name: "1x" })).toMatch(/name/);
    expect(stdioProblem({ ...good, packageName: " " })).toMatch(/package/);
    expect(stdioProblem({ ...good, version: "^1.2.0" })).toMatch(/exact version/);
    expect(stdioProblem({ ...good, version: "latest" })).toMatch(/exact version/);
    for (const version of ["1", "1.2", "1.x", "1.2.x"]) {
      expect(stdioProblem({ ...good, version })).toMatch(/exact version/);
    }
    expect(stdioProblem({ ...good, version: "1.2.3-beta.1+build.5" })).toBeNull();
    expect(stdioProblem({ ...good, source: "pypi", packageName: "mcp-server-fetch", version: "1.2" })).toBeNull();
    expect(stdioProblem({ ...good, source: "pypi", packageName: "mcp-server-fetch", version: "1.*" })).toMatch(/exact version/);
    expect(stdioProblem({ ...good, env: "GITHUB_TOKEN, lower" })).toMatch(/"lower" is not a variable/);
    expect(stdioProblem({ ...good, egress: "api.github.com\nhttps://x.test" })).toMatch(/not a host name/);
    expect(stdioProblem({ ...good, egress: "localhost" })).toMatch(/not a host name/);
    expect(stdioProblem({ ...good, memoryMb: "32" })).toMatch(/64 to 4096/);
    expect(stdioProblem({ ...good, idleS: "abc" })).toMatch(/60 to 3600/);
    expect(stdioProblem({ ...good, source: "command", command: " \n " })).toMatch(/command/);
  });

  it("makes the body of a package or a command", () => {
    expect(stdioBody({ ...good, env: "A_KEY\nB_KEY", egress: "API.Example.com", args: "stdio\n", memoryMb: "512" })).toEqual({
      name: "everything",
      title: null,
      scope: "workspace",
      env: ["A_KEY", "B_KEY"],
      egress: ["api.example.com"],
      memory_mb: 512,
      idle_s: null,
      package: {
        registry: "npm",
        name: "@modelcontextprotocol/server-everything",
        version: "2025.9.25",
        bin: null,
        args: ["stdio"],
      },
    });
    const command = stdioBody({ ...good, source: "command", command: "python3\n-m\nserver" });
    expect(command.command).toEqual(["python3", "-m", "server"]);
    expect(command.package).toBeUndefined();
  });

  it("makes a card with one secret field for each variable", () => {
    const hosted = {
      title: null,
      package: { registry: "pypi" as const, name: "mcp-server-fetch", version: "1.0.0" },
      command: null,
      env: ["API_TOKEN"],
      egress: [],
      memory_mb: 384,
      idle_s: 600,
    };
    const mcp = { custom: hosted, include: [], exclude: [], discovered_at: null, tools: [] };
    const cards = customServers([connection({ connector: "fetch", kind: "mcp_hosted", scope: "workspace", mcp })], []);
    expect(cards[0]?.kind).toBe("mcp_hosted");
    expect(cards[0]?.description).toContain("mcp-server-fetch==1.0.0 (PyPI)");
    expect(cards[0]?.fields).toEqual(hostedFields(["API_TOKEN"]));
    expect(hostedFields(["API_TOKEN"])[0]).toMatchObject({ name: "api_token", label: "API_TOKEN", secret: true });
    expect(hostedWhat({ package: null, command: ["node", "server.js"] })).toBe("the command node server.js");
    expect(toolSummary(cards[0]!)).toMatch(/connector host/);
  });
});

describe("HTTP APIs (M4.12)", () => {
  const good = { ...EMPTY_API, name: "Shop", openapi: "openapi: 3.0.3" };

  it("reads a list of endpoints", () => {
    expect(parseEndpoints("GET /items?limit,cursor list_items\n post /items/{id}\nDELETE /items/{id} drop")).toEqual([
      { name: "list_items", method: "GET", path: "/items", query: ["limit", "cursor"], body: false },
      { name: null, method: "POST", path: "/items/{id}", query: [], body: true },
      { name: "drop", method: "DELETE", path: "/items/{id}", query: [], body: false },
    ]);
    expect(parseEndpoints("FETCH /items")).toMatch(/not an endpoint/);
    expect(parseEndpoints("GET items")).toMatch(/not an endpoint/);
    expect(parseEndpoints("GET /items name extra")).toMatch(/not an endpoint/);
    expect(parseEndpoints("GET /items?$x")).toMatch(/query parameter/);
    expect(parseEndpoints(" ")).toMatch(/one endpoint/);
  });

  it("checks the form", () => {
    expect(apiProblem(good)).toBeNull();
    expect(apiProblem({ ...good, openapi: "" })).toMatch(/Paste/);
    expect(apiProblem({ ...good, baseUrl: "http://x.test" })).toMatch(/https/);
    expect(apiProblem({ ...good, source: "endpoints", endpoints: "GET /a" })).toMatch(/base address/);
    expect(apiProblem({ ...good, source: "endpoints", endpoints: "GET /a", baseUrl: "https://x.test/v1" })).toBeNull();
    expect(apiProblem({ ...good, source: "openapi_url", openapiUrl: "ftp://x" })).toMatch(/https/);
    expect(apiProblem({ ...good, keyName: "Content-Type" })).toMatch(/sets the Content-Type/);
    expect(apiProblem({ ...good, keyName: "api_key", keyLocation: "query" })).toBeNull();
    expect(apiProblem({ ...good, auth: "oauth2", tokenUrl: "" })).toMatch(/token URL/);
  });

  it("makes the body for each sign-in", () => {
    expect(apiBody(good)).toEqual({
      name: "shop",
      title: null,
      scope: "workspace",
      base_url: null,
      auth: { type: "api_key", name: "X-Api-Key", location: "header" },
      openapi: "openapi: 3.0.3",
    });
    const oauth = apiBody({
      ...good,
      auth: "oauth2",
      tokenUrl: " https://auth.test/token ",
      oauthScope: "read",
      source: "endpoints",
      endpoints: "GET /a",
      baseUrl: "https://x.test",
    });
    expect(oauth.auth).toEqual({
      type: "oauth2",
      token_url: "https://auth.test/token",
      scope: "read",
      client_auth: "basic",
    });
    const inBody = apiBody({ ...good, auth: "oauth2", tokenUrl: "https://auth.test/token", clientAuth: "body" });
    expect(inBody.auth).toEqual({ type: "oauth2", token_url: "https://auth.test/token", client_auth: "body" });
    expect(oauth.endpoints).toHaveLength(1);
    expect(oauth.openapi).toBeUndefined();
    expect(apiBody({ ...good, auth: "bearer" }).auth).toEqual({ type: "bearer" });
  });

  it("makes the same form fields as the API", () => {
    expect(apiFields("api_key", "X-Key", "query")[0]?.help).toBe("The key goes into the X-Key query parameter.");
    expect(apiFields("bearer").map((f) => f.name)).toEqual(["token"]);
    expect(apiFields("basic").map((f) => [f.name, f.secret])).toEqual([
      ["username", false],
      ["password", true],
    ]);
    expect(apiFields("oauth2").map((f) => [f.name, f.secret])).toEqual([
      ["client_id", false],
      ["client_secret", true],
    ]);
    expect(apiFields("none")).toEqual([]);
  });

  it("makes a card for an API that an admin added, and shows the tools", () => {
    const custom = {
      title: "Shop",
      base_url: "https://shop.test/v1",
      auth: "basic" as const,
      auth_name: null,
      auth_location: null,
      token_url: null,
    };
    const tools = [
      { name: "api_shop_list", remote_name: "list", description: "", access: "read" as const, enabled: true, method: "GET", path: "/items" },
      { name: "api_shop_add", remote_name: "add", description: "", access: "write" as const, enabled: true, method: "POST", path: "/items" },
    ];
    const mcp = { custom, include: [], exclude: [], discovered_at: "2026-11-20T00:00:00Z", tools };
    const row = connection({ connector: "shop", kind: "http_api", scope: "workspace", state: "connected", mcp });
    const cards = customServers([row], [NOTION]);
    expect(cards[0]).toMatchObject({ kind: "http_api", title: "Shop", api: { base_url: "https://shop.test/v1", auth: "basic" } });
    expect(cards[0]?.fields.map((f) => f.name)).toEqual(["username", "password"]);
    expect(toolSummary(cards[0]!)).toBe("HTTP API");
    expect(mcpToolSummary(row)).toBe("2 tools, 1 read only");
    // Each connection keeps its own API: the form follows the auth of that connection.
    const mine = connection({
      id: "y",
      connector: "shop",
      kind: "http_api",
      scope: "personal",
      mcp: { ...mcp, custom: { ...custom, base_url: "https://shop.test/v2", auth: "bearer" as const } },
    });
    const [card] = customServers([row, mine], [NOTION]);
    if (card === undefined) throw new Error("no card");
    expect(card.description).toBe("HTTP API at https://shop.test/v1, https://shop.test/v2");
    expect(connectorFor(card, mine).fields.map((f) => f.name)).toEqual(["token"]);
    expect(connectorFor(card, mine).api).toEqual({ base_url: "https://shop.test/v2", auth: "bearer" });
  });

  it("calls the add routes", async () => {
    const fetcher = vi.fn(async () => new Response("{}", { status: 201, headers: { "content-type": "application/json" } }));
    const client = new ApiClient("https://api.test", tokens(), fetcher);
    await client.addStdioServer(WS, { name: "e", scope: "workspace", command: ["x"], env: [], egress: [] });
    await client.addApi(WS, { name: "s", scope: "workspace", auth: { type: "none" }, openapi_url: "https://s.test/o.json" });
    const urls = fetcher.mock.calls.map((call) => (call as unknown as [string])[0]);
    expect(urls).toEqual([`https://api.test/workspaces/${WS}/connections/stdio`, `https://api.test/workspaces/${WS}/connections/api`]);
  });
});


function tokens(): TokenSource {
  return { generation: 1, getToken: async () => "t", refresh: async () => "t", signOut: () => undefined };
}
