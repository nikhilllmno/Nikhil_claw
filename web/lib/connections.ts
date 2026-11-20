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
  Connection,
  ConnectionScope,
  ConnectionState,
  Connector,
  ConnectorField,
  McpAuth,
  McpServerAdd,
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

/** The text of the first problem of a new server, or `null` if the form can be sent. */
export function serverProblem(draft: ServerDraft): string | null {
  if (!CONNECTOR.test(draft.name.trim().toLowerCase())) {
    return "The name has 1 to 40 lower-case letters, digits, _ and -, and starts with a letter.";
  }
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
 * The servers that an admin added by URL, as connectors: they are not in the catalog, so the
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
      if (!seen.description.includes(custom.url)) seen.description += `, ${custom.url}`;
      continue;
    }
    out.set(connection.connector, {
      name: connection.connector,
      title: custom.title ?? connection.connector,
      description: `Added by URL: ${custom.url}`,
      kind: "mcp_remote",
      scopes: [connection.scope],
      fields: mcpFields(custom.auth, custom.header),
      tools: [],
      mcp: { url: custom.url, auth: custom.auth, header: custom.header },
    });
  }
  return [...out.values()].sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * The connector for the form of one connection. A server by URL has its address and its
 * credential kind on each connection, so a personal and a workspace connection with the same
 * name can need different fields.
 */
export function connectorFor(connector: Connector, connection: Connection | null): Connector {
  const custom = connection?.mcp?.custom;
  if (!custom) return connector;
  return {
    ...connector,
    fields: mcpFields(custom.auth, custom.header),
    mcp: { url: custom.url, auth: custom.auth, header: custom.header },
  };
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
