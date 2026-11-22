// The logic of the Connections page (docs/architecture.md, Section 10.3), without React.
// The page has two entry links:
// - `/connections?workspace=<id>&connect=<name>&scope=<scope>` from `/connect` in a chat or from
//   the tool `connection_request`. The page starts the connection.
// - `/connections?workspace=<id>&connection=<id>` from the Composio sign-in. The page asks the
//   API to check the connection. Only the API sets the state `connected`.
// - `/connections?code=<code>&state=<state>` (or `?error=<error>&state=<state>`) from the OAuth
//   sign-in of a remote MCP server. The page gives both to the API, which compares the state
//   with the one that it stored.

import type {
  ApiAdd,
  ApiAuthType,
  ApiCustom,
  ApiEndpoint,
  Connection,
  ConnectionScope,
  ConnectionState,
  Connector,
  ConnectorField,
  HostedCustom,
  McpAuth,
  McpCustom,
  McpServerAdd,
  StdioServerAdd,
} from "./api";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
/** The same rule as the `ck_connections_connector` CHECK of the database. */
const CONNECTOR = /^[a-z][a-z0-9_-]{0,39}$/;

/** The OAuth state of sama: `<connection id>.<random>`. */
const OAUTH_STATE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.[A-Za-z0-9_-]{16,160}$/i;

export type ConnectionIntent =
  | { kind: "connect"; workspaceId: string; connector: string; scope: ConnectionScope }
  | { kind: "callback"; workspaceId: string; connectionId: string }
  | { kind: "oauth"; state: string; code: string | null; error: string | null };

/** The action of an entry link, or `null` if the query has no valid action. */
export function parseConnectionQuery(search: string): ConnectionIntent | null {
  const query = new URLSearchParams(search);
  const state = query.get("state");
  if (state !== null) {
    const code = query.get("code");
    const error = query.get("error");
    if (!OAUTH_STATE.test(state) || (code === null) === (error === null)) return null;
    if ((code?.length ?? 0) > 4000 || (error?.length ?? 0) > 200) return null;
    return { kind: "oauth", state, code, error };
  }
  const workspaceId = query.get("workspace") ?? "";
  if (!UUID.test(workspaceId)) return null;
  const connectionId = query.get("connection");
  if (connectionId !== null) {
    return UUID.test(connectionId) ? { kind: "callback", workspaceId, connectionId } : null;
  }
  const connector = query.get("connect") ?? "";
  if (!CONNECTOR.test(connector)) return null;
  const scope = query.get("scope") ?? "personal";
  if (scope !== "personal" && scope !== "workspace") return null;
  return { kind: "connect", workspaceId, connector, scope };
}

/**
 * True only for an `https` address without a user name or a password. The page goes to a
 * sign-in link only if this is true. The link comes from the API (Composio), not from the
 * query of the page.
 */
export function isSafeRedirect(url: string | null): url is string {
  if (url === null) return false;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && parsed.username === "" && parsed.password === "";
  } catch {
    return false;
  }
}

/** The connections of one workspace. The page shows them only for that workspace. */
export type ConnectionList = { workspaceId: string; items: Connection[] };

/** The connections to show for the workspace, or `null` while they load. */
export function listFor(list: ConnectionList | null, workspaceId: string): Connection[] | null {
  return list !== null && list.workspaceId === workspaceId ? list.items : null;
}

/**
 * The list with the saved connection in it. A connection of another workspace (an old answer
 * after the user changed the workspace) does not go into the list.
 */
export function withConnection(list: ConnectionList | null, saved: Connection): ConnectionList | null {
  if (list === null || list.workspaceId !== saved.workspace_id) return list;
  return { ...list, items: [...list.items.filter((c) => c.id !== saved.id), saved] };
}

/** The list without the removed connection. */
export function withoutConnection(list: ConnectionList | null, id: string): ConnectionList | null {
  return list === null ? null : { ...list, items: list.items.filter((c) => c.id !== id) };
}

const STATE_LABELS: Record<string, string> = {
  pending: "Fill in the form",
  initiated: "Waiting for sign-in",
  connected: "Connected",
  failed: "Failed",
  expired: "Expired",
  disabled: "Disabled",
};

export function stateLabel(state: ConnectionState): string {
  return STATE_LABELS[state] ?? state;
}

/** The tone of the state badge. */
export function stateTone(state: ConnectionState): "ok" | "wait" | "error" | "off" {
  if (state === "connected") return "ok";
  if (state === "pending" || state === "initiated") return "wait";
  if (state === "disabled") return "off";
  return "error";
}

/**
 * The connection of a connector with this scope that the user can see: the personal one of
 * the user, or the workspace one.
 */
export function connectionOf(
  connections: readonly Connection[],
  connector: string,
  scope: ConnectionScope,
): Connection | null {
  return connections.find((c) => c.connector === connector && c.scope === scope) ?? null;
}

/** The text of the first problem of a form, or `null` if the form can be sent. */
export function formProblem(
  fields: readonly ConnectorField[],
  values: Readonly<Record<string, string>>,
): string | null {
  for (const field of fields) {
    if (field.required && (values[field.name] ?? "").trim() === "") {
      return `Fill in ${field.label}.`;
    }
  }
  return null;
}

/** The values to send: the trimmed values of the fields of the connector, without empty ones. */
export function formValues(
  fields: readonly ConnectorField[],
  values: Readonly<Record<string, string>>,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const field of fields) {
    const value = (values[field.name] ?? "").trim();
    if (value !== "") out[field.name] = value;
  }
  return out;
}

/** The start values of a form: the saved fields that are not secret. Secrets start empty. */
export function initialValues(
  connector: Connector,
  connection: Connection | null,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const field of connector.fields) {
    out[field.name] = field.secret ? "" : (connection?.fields[field.name] ?? "");
  }
  return out;
}

/** A short line for the tools of a connector, for example "3 read, 2 write". */
export function toolSummary(connector: Connector): string {
  if (connector.kind === "mcp_remote") return "Remote MCP server";
  if (connector.kind === "mcp_hosted") return "Stdio MCP server in a connector host";
  if (connector.api && connector.tools.length === 0) return "HTTP API";
  const read = connector.tools.filter((t) => t.access === "read").length;
  const write = connector.tools.length - read;
  const parts = [`${read} read`];
  if (write > 0) parts.push(`${write} write (asks first)`);
  return parts.join(", ");
}

const ENTRY_KEYS = ["workspace", "connect", "scope", "connection"] as const;
/** The OAuth answer: the code is used once, and it must not stay in the address or history. */
const OAUTH_KEYS = ["code", "state", "error", "error_description", "error_uri", "iss"] as const;

/** True if the query has a part of an OAuth answer, valid or not. */
export function hasOAuthAnswer(search: string): boolean {
  const query = new URLSearchParams(search);
  return OAUTH_KEYS.some((key) => query.has(key));
}

/** The query of a page address without the entry-link parameters. */
export function withoutEntryParams(search: string): string {
  const query = new URLSearchParams(search);
  for (const key of [...ENTRY_KEYS, ...OAUTH_KEYS]) query.delete(key);
  const rest = query.toString();
  return rest === "" ? "" : `?${rest}`;
}

// -- remote MCP servers (M3.5) ------------------------------------------------------------------

/** The same rules as the API (`connectors/manifest.py`). The name rule is `CONNECTOR`. */
const HEADER = /^[A-Za-z][A-Za-z0-9-]{0,63}$/;
const TOOL_PATTERN = /^[A-Za-z0-9_.*?/:-]{1,100}$/;
const MAX_PATTERNS = 50;
/** The headers that sama sets itself. A server by URL cannot use them for its credential. */
const RESERVED_HEADERS = new Set([
  "accept",
  "accept-encoding",
  "connection",
  "content-length",
  "content-type",
  "host",
  "mcp-protocol-version",
  "mcp-session-id",
  "transfer-encoding",
]);

/** The form of a new server by URL, as the user types it. */
export type ServerDraft = {
  name: string;
  title: string;
  url: string;
  auth: McpAuth;
  header: string;
  oauthScope: string;
  scope: ConnectionScope;
};

export const EMPTY_SERVER: ServerDraft = {
  name: "",
  title: "",
  url: "",
  auth: "oauth",
  header: "",
  oauthScope: "",
  scope: "workspace",
};

const NAME_PROBLEM =
  "The name has 1 to 40 lower-case letters, digits, _ and -, and starts with a letter.";

/** The text of the first problem of a new server, or `null` if the form can be sent. */
export function serverProblem(draft: ServerDraft): string | null {
  if (!CONNECTOR.test(draft.name.trim().toLowerCase())) return NAME_PROBLEM;
  let url: URL;
  try {
    url = new URL(draft.url.trim());
  } catch {
    return "Give the full address of the server, for example https://mcp.example.com/mcp.";
  }
  if (url.protocol !== "https:") return "The address must start with https://.";
  if (url.username !== "" || url.password !== "") return "The address cannot have a user name or a password.";
  if (url.search !== "" || url.hash !== "") return "The address cannot have a query or a fragment.";
  if (draft.auth === "header") {
    const header = draft.header.trim();
    if (!HEADER.test(header)) return "Give the name of the header, for example X-Api-Key.";
    if (RESERVED_HEADERS.has(header.toLowerCase())) return `sama sets the ${header} header itself.`;
  }
  return null;
}

/** The body of `POST /workspaces/{id}/connections/mcp`. */
export function serverBody(draft: ServerDraft): McpServerAdd {
  const title = draft.title.trim();
  const oauthScope = draft.oauthScope.trim();
  return {
    name: draft.name.trim().toLowerCase(),
    url: draft.url.trim(),
    auth: draft.auth,
    header: draft.auth === "header" ? draft.header.trim() : null,
    oauth_scope: draft.auth === "oauth" && oauthScope !== "" ? oauthScope : null,
    title: title === "" ? null : title,
    scope: draft.scope,
  };
}

/**
 * The servers and APIs that an admin added, as connectors: they are not in the catalog, so the
 * page makes a card for each from its connections.
 */
export function customServers(
  connections: readonly Connection[],
  catalog: readonly Connector[],
): Connector[] {
  const known = new Set(catalog.map((c) => c.name));
  const out = new Map<string, Connector>();
  for (const connection of connections) {
    const custom = connection.mcp?.custom;
    if (!custom || known.has(connection.connector)) continue;
    const seen = out.get(connection.connector);
    if (seen) {
      if (!seen.scopes.includes(connection.scope)) seen.scopes.push(connection.scope);
      // Each connection keeps its own server: the description names all addresses.
      const where = customWhere(connection.kind, custom);
      if (where && !seen.description.includes(where)) seen.description += `, ${where}`;
      continue;
    }
    out.set(connection.connector, customCard(connection, custom));
  }
  return [...out.values()].sort((a, b) => a.name.localeCompare(b.name));
}

function customCard(connection: Connection, custom: McpCustom | HostedCustom | ApiCustom): Connector {
  const base = { name: connection.connector, title: custom.title ?? connection.connector, scopes: [connection.scope], tools: [] };
  if (connection.kind === "mcp_hosted") {
    const hosted = custom as HostedCustom;
    return {
      ...base,
      description: `Runs in a connector host: ${hostedWhat(hosted)}`,
      kind: "mcp_hosted",
      fields: hostedFields(hosted.env),
      hosted,
    };
  }
  if (connection.kind === "http_api") {
    const api = custom as ApiCustom;
    return {
      ...base,
      description: `HTTP API at ${api.base_url ?? ""}`,
      kind: "http_api",
      fields: apiFields(api.auth ?? "none", api.auth_name, api.auth_location),
      api: { base_url: api.base_url ?? "", auth: api.auth ?? "none" },
    };
  }
  const mcp = custom as McpCustom;
  return {
    ...base,
    description: `Added by URL: ${mcp.url}`,
    kind: "mcp_remote",
    fields: mcpFields(mcp.auth, mcp.header),
    mcp: { url: mcp.url, auth: mcp.auth, header: mcp.header },
  };
}

/** The address of a custom server or API, or `null` for a connector host. */
function customWhere(kind: string, custom: McpCustom | HostedCustom | ApiCustom): string | null {
  if (kind === "mcp_remote") return (custom as McpCustom).url;
  if (kind === "http_api") return (custom as ApiCustom).base_url;
  return null;
}

/** What a connector host runs: the pinned package, or the custom command. */
export function hostedWhat(hosted: Pick<HostedCustom, "package" | "command">): string {
  if (hosted.package) {
    const { registry, name, version } = hosted.package;
    return registry === "npm" ? `${name}@${version} (npm)` : `${name}==${version} (PyPI)`;
  }
  return `the command ${(hosted.command ?? []).join(" ")}`;
}

/** The form of a stdio server: one secret field for each variable, as `hosted_fields` of the API. */
export function hostedFields(env: readonly string[]): ConnectorField[] {
  return env.map((name) => ({
    name: name.toLowerCase(),
    label: name,
    secret: true,
    required: true,
    help: `The server gets the value as the environment variable ${name}.`,
  }));
}

/** The form of an HTTP API: the same fields as `api_fields` of the API. */
export function apiFields(
  auth: ApiAuthType,
  name: string | null = null,
  location: "header" | "query" | null = "header",
): ConnectorField[] {
  const field = (n: string, label: string, secret: boolean, help = ""): ConnectorField => ({
    name: n,
    label,
    secret,
    required: true,
    help,
  });
  if (auth === "api_key") {
    const where = location === "query" ? "query parameter" : "header";
    return [field("api_key", "API key", true, `The key goes into the ${name ?? ""} ${where}.`)];
  }
  if (auth === "bearer") return [field("token", "Bearer token", true, "The token goes into the Authorization header.")];
  if (auth === "basic") return [field("username", "User name", false), field("password", "Password", true)];
  if (auth === "oauth2") {
    return [
      field("client_id", "Client ID", false),
      field("client_secret", "Client secret", true, "sama gets a token from the token URL with the client credentials grant."),
    ];
  }
  return [];
}

/**
 * The connector for the form of one connection. A custom server or API has its address and
 * its credential kind on each connection, so a personal and a workspace connection with the same
 * name can need different fields.
 */
export function connectorFor(connector: Connector, connection: Connection | null): Connector {
  const custom = connection?.mcp?.custom;
  if (!connection || !custom) return connector;
  const card = customCard(connection, custom);
  return { ...connector, fields: card.fields, mcp: card.mcp, hosted: card.hosted, api: card.api };
}

/**
 * True if the page offers a check of the connection. A `pending` connection waits for its form,
 * so a check does not help. A remote MCP server without a form (OAuth, or no auth) is `pending`
 * after its sign-in if the server did not answer; a check then finishes it.
 */
export function canCheck(connector: Connector, connection: Connection): boolean {
  if (connection.state !== "pending") return true;
  return connection.mcp != null && connectorFor(connector, connection).fields.length === 0;
}

/** The form of a server: the same field as `mcp_fields` of the API. */
export function mcpFields(auth: McpAuth, header: string | null): ConnectorField[] {
  if (auth === "bearer") {
    return [{ name: "token", label: "Access token", secret: true, required: true, help: "The token goes into the Authorization header." }];
  }
  if (auth === "header") {
    return [{ name: "token", label: "Header value", secret: true, required: true, help: `The value goes into the ${header ?? ""} header.` }];
  }
  return [];
}

/** A short line for the tools of a connected server, for example "12 tools, 4 read only, 2 off". */
export function mcpToolSummary(connection: Connection): string | null {
  const tools = connection.mcp?.tools;
  if (!tools || connection.mcp?.discovered_at == null) return null;
  const on = tools.filter((t) => t.enabled);
  const parts = [`${on.length} ${on.length === 1 ? "tool" : "tools"}`, `${on.filter((t) => t.access === "read").length} read only`];
  const off = tools.length - on.length;
  if (off > 0) parts.push(`${off} off`);
  return parts.join(", ");
}

/** The patterns of a filter: one for each line or comma. */
export function parsePatterns(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((p) => p.trim())
    .filter((p) => p !== "");
}

/** The text of the first bad pattern, or `null` if all are good. */
export function patternProblem(patterns: readonly string[]): string | null {
  if (patterns.length > MAX_PATTERNS) return `Give ${MAX_PATTERNS} patterns or fewer.`;
  const bad = patterns.find((p) => !TOOL_PATTERN.test(p));
  return bad === undefined ? null : `"${bad}" is not a pattern. Use letters, digits, and _ . * ? / : -.`;
}

// -- stdio MCP servers in a connector host (M4.11) ----------------------------------------------

const ENV_NAME = /^[A-Z][A-Z0-9_]{0,39}$/;
const HOST_NAME = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/;
/** One exact version for each registry: not a range, a tag, or `latest`. npm needs full semver,
 * because npm reads `1`, `1.2`, and `1.x` as ranges. */
const EXACT_VERSION: Record<"npm" | "pypi", RegExp> = {
  npm: /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$/,
  pypi: /^v?([0-9]+!)?[0-9]+(\.[0-9]+)*([-_.]?(alpha|a|beta|b|preview|pre|c|rc)[-_.]?[0-9]*)?(-[0-9]+|[-_.]?(post|rev|r)[-_.]?[0-9]*)?([-_.]?dev[-_.]?[0-9]*)?(\+[a-z0-9]+([-_.][a-z0-9]+)*)?$/i,
};

/** The form of a new stdio server, as the user types it. */
export type StdioDraft = {
  name: string;
  title: string;
  /** `command` is a custom command. Only the org owner can add one. */
  source: "npm" | "pypi" | "command";
  packageName: string;
  version: string;
  bin: string;
  /** One argument on each line. */
  args: string;
  /** The program and its arguments, one on each line. */
  command: string;
  /** The names of the environment variables, one on each line or with commas. */
  env: string;
  /** The host names that the server can reach, one on each line or with commas. */
  egress: string;
  memoryMb: string;
  idleS: string;
  scope: ConnectionScope;
};

export const EMPTY_STDIO: StdioDraft = {
  name: "",
  title: "",
  source: "npm",
  packageName: "",
  version: "",
  bin: "",
  args: "",
  command: "",
  env: "",
  egress: "",
  memoryMb: "",
  idleS: "",
  scope: "workspace",
};

function lines(text: string): string[] {
  return text
    .split("\n")
    .map((p) => p.trim())
    .filter((p) => p !== "");
}

function whole(text: string): number | null {
  const value = text.trim();
  return /^[0-9]{1,6}$/.test(value) ? Number(value) : null;
}

/** `true` if the text is not empty and is not a whole number from `min` to `max`. */
function outside(text: string, min: number, max: number): boolean {
  const value = whole(text);
  return text.trim() !== "" && (value === null || value < min || value > max);
}

/** The text of the first problem of a new stdio server, or `null` if the form can be sent. */
export function stdioProblem(draft: StdioDraft): string | null {
  if (!CONNECTOR.test(draft.name.trim().toLowerCase())) return NAME_PROBLEM;
  if (draft.source === "command") {
    if (lines(draft.command).length === 0) return "Give the command: the program on the first line.";
  } else {
    if (draft.packageName.trim() === "") return "Give the name of the package.";
    if (!EXACT_VERSION[draft.source].test(draft.version.trim())) {
      return "Give one exact version of the package, for example 1.2.3. A range or latest is not permitted.";
    }
  }
  const bad = parsePatterns(draft.env).find((name) => !ENV_NAME.test(name));
  if (bad !== undefined) return `"${bad}" is not a variable name. Use A to Z, 0 to 9, and _.`;
  const host = parsePatterns(draft.egress).find((h) => !HOST_NAME.test(h.toLowerCase()));
  if (host !== undefined) return `"${host}" is not a host name, for example api.example.com.`;
  if (outside(draft.memoryMb, 64, 4096)) return "The memory limit is 64 to 4096 MB.";
  if (outside(draft.idleS, 60, 3600)) return "The idle time is 60 to 3600 seconds.";
  return null;
}

/** The body of `POST /workspaces/{id}/connections/stdio`. */
export function stdioBody(draft: StdioDraft): StdioServerAdd {
  const title = draft.title.trim();
  const body: StdioServerAdd = {
    name: draft.name.trim().toLowerCase(),
    title: title === "" ? null : title,
    scope: draft.scope,
    env: parsePatterns(draft.env),
    egress: parsePatterns(draft.egress).map((h) => h.toLowerCase()),
    memory_mb: whole(draft.memoryMb),
    idle_s: whole(draft.idleS),
  };
  if (draft.source === "command") {
    body.command = lines(draft.command);
  } else {
    const bin = draft.bin.trim();
    body.package = {
      registry: draft.source,
      name: draft.packageName.trim(),
      version: draft.version.trim(),
      bin: bin === "" ? null : bin,
      args: lines(draft.args),
    };
  }
  return body;
}

// -- HTTP APIs (M4.12) --------------------------------------------------------------------------

const METHODS = ["GET", "PUT", "POST", "DELETE", "PATCH", "HEAD"] as const;
const WITH_BODY = new Set(["PUT", "POST", "PATCH"]);
const PARAM = /^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/;
/** The same rules as `ApiAuth` of the API. */
const KEY_NAME = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/;
const API_RESERVED_HEADERS = new Set([
  "accept",
  "accept-encoding",
  "content-length",
  "content-type",
  "host",
  "transfer-encoding",
]);

/** The form of a new HTTP API, as the user types it. */
export type ApiDraft = {
  name: string;
  title: string;
  baseUrl: string;
  auth: ApiAuthType;
  /** The header or query parameter of an API key. */
  keyName: string;
  keyLocation: "header" | "query";
  tokenUrl: string;
  oauthScope: string;
  /** How an OAuth 2 client sends its id and secret to the token URL. */
  clientAuth: "basic" | "body";
  /** Where the operations come from. */
  source: "openapi" | "openapi_url" | "endpoints";
  openapi: string;
  openapiUrl: string;
  /** One endpoint on each line: `METHOD /path?query,params name`. */
  endpoints: string;
  scope: ConnectionScope;
};

export const EMPTY_API: ApiDraft = {
  name: "",
  title: "",
  baseUrl: "",
  auth: "api_key",
  keyName: "X-Api-Key",
  keyLocation: "header",
  tokenUrl: "",
  oauthScope: "",
  clientAuth: "basic",
  source: "openapi",
  openapi: "",
  openapiUrl: "",
  endpoints: "",
  scope: "workspace",
};

/**
 * The endpoints of the text, one on each line: `GET /items/{id}`, `GET /items?limit,cursor
 * list_items`, or `POST /items create_item`. A line that is not an endpoint is an error.
 */
export function parseEndpoints(text: string): ApiEndpoint[] | string {
  const out: ApiEndpoint[] = [];
  for (const line of lines(text)) {
    const [method = "", target = "", name, ...rest] = line.split(/\s+/);
    const upper = method.toUpperCase() as (typeof METHODS)[number];
    if (!METHODS.includes(upper) || !target.startsWith("/") || rest.length > 0) {
      return `"${line}" is not an endpoint. Write the method, the path, and an optional name.`;
    }
    const [path = "", query = ""] = target.split("?", 2);
    const params = query.split(",").map((p) => p.trim()).filter((p) => p !== "");
    const bad = params.find((p) => !PARAM.test(p));
    if (bad !== undefined) return `"${bad}" is not a query parameter name.`;
    out.push({ name: name ?? null, method: upper, path, query: params, body: WITH_BODY.has(upper) });
  }
  if (out.length === 0) return "Give one endpoint or more.";
  return out;
}

function httpsProblem(text: string, what: string): string | null {
  let url: URL;
  try {
    url = new URL(text.trim());
  } catch {
    return `Give the full ${what}, for example https://api.example.com/v1.`;
  }
  if (url.protocol !== "https:") return `The ${what} must start with https://.`;
  if (url.username !== "" || url.password !== "") return `The ${what} cannot have a user name or a password.`;
  return null;
}

/** The text of the first problem of a new HTTP API, or `null` if the form can be sent. */
export function apiProblem(draft: ApiDraft): string | null {
  if (!CONNECTOR.test(draft.name.trim().toLowerCase())) return NAME_PROBLEM;
  if (draft.baseUrl.trim() !== "" || draft.source === "endpoints") {
    const problem = httpsProblem(draft.baseUrl, "base address");
    if (problem !== null) return problem;
  }
  if (draft.auth === "api_key") {
    const key = draft.keyName.trim();
    if (!KEY_NAME.test(key)) return "Give the name of the header or query parameter of the key.";
    if (draft.keyLocation === "header" && API_RESERVED_HEADERS.has(key.toLowerCase())) {
      return `sama sets the ${key} header itself.`;
    }
  }
  if (draft.auth === "oauth2") {
    const problem = httpsProblem(draft.tokenUrl, "token URL");
    if (problem !== null) return problem;
  }
  if (draft.source === "openapi" && draft.openapi.trim() === "") return "Paste the OpenAPI document.";
  if (draft.source === "openapi_url") return httpsProblem(draft.openapiUrl, "address of the document");
  if (draft.source === "endpoints") {
    const endpoints = parseEndpoints(draft.endpoints);
    if (typeof endpoints === "string") return endpoints;
  }
  return null;
}

/** The body of `POST /workspaces/{id}/connections/api`. Call it only after `apiProblem`. */
export function apiBody(draft: ApiDraft): ApiAdd {
  const title = draft.title.trim();
  const baseUrl = draft.baseUrl.trim();
  const auth: ApiAdd["auth"] = { type: draft.auth };
  if (draft.auth === "api_key") {
    auth.name = draft.keyName.trim();
    auth.location = draft.keyLocation;
  }
  if (draft.auth === "oauth2") {
    auth.token_url = draft.tokenUrl.trim();
    const scope = draft.oauthScope.trim();
    if (scope !== "") auth.scope = scope;
    auth.client_auth = draft.clientAuth;
  }
  const body: ApiAdd = {
    name: draft.name.trim().toLowerCase(),
    title: title === "" ? null : title,
    scope: draft.scope,
    base_url: baseUrl === "" ? null : baseUrl,
    auth,
  };
  if (draft.source === "openapi") body.openapi = draft.openapi;
  if (draft.source === "openapi_url") body.openapi_url = draft.openapiUrl.trim();
  if (draft.source === "endpoints") {
    const endpoints = parseEndpoints(draft.endpoints);
    body.endpoints = typeof endpoints === "string" ? [] : endpoints;
  }
  return body;
}


/** The order of the kinds on the Connections page: the apps of a user first (M8.1). */
const KIND_ORDER: Record<string, number> = { composio: 0, mcp_remote: 1, mcp_hosted: 2, http_api: 3, database: 4 };

/** The connectors in page order: by kind, then by title. `query` keeps the ones that match. */
export function orderConnectors<T extends { name: string; title: string; kind: string; description: string }>(
  connectors: T[],
  query = "",
): T[] {
  const words = query.trim().toLowerCase();
  return connectors
    .filter(
      (c) =>
        words === "" ||
        c.name.toLowerCase().includes(words) ||
        c.title.toLowerCase().includes(words) ||
        c.description.toLowerCase().includes(words),
    )
    .sort(
      (a, b) =>
        (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9) || a.title.localeCompare(b.title),
    );
}

/** The text under a connection that is not connected, so no row stays without a reason. */
export function stuckHint(state: string, kind: string): string | null {
  if (state === "initiated") {
    return kind === "composio" || kind === "mcp_remote"
      ? "The sign-in is open. Finish it in the tab of the provider, or retry."
      : null;
  }
  if (state === "expired") return "The sign-in ended before it finished. Retry to connect.";
  return null;
}

/** The label of the connect button of a row. */
export function connectLabel(state: string | null): string {
  if (state === null) return "Connect";
  return state === "initiated" || state === "failed" || state === "expired" ? "Retry" : "Connect again";
}
