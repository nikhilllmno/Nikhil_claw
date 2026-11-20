// The logic of the Connections page (docs/architecture.md, Section 10.3), without React.
// The page has two entry links:
// - `/connections?workspace=<id>&connect=<name>&scope=<scope>` from `/connect` in a chat or from
//   the tool `connection_request`. The page starts the connection.
// - `/connections?workspace=<id>&connection=<id>` from the Composio sign-in. The page asks the
//   API to check the connection. Only the API sets the state `connected`.

import type { Connection, ConnectionScope, ConnectionState, Connector, ConnectorField } from "./api";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
/** The same rule as the `ck_connections_connector` CHECK of the database. */
const CONNECTOR = /^[a-z][a-z0-9_-]{0,39}$/;

export type ConnectionIntent =
  | { kind: "connect"; workspaceId: string; connector: string; scope: ConnectionScope }
  | { kind: "callback"; workspaceId: string; connectionId: string };

/** The action of an entry link, or `null` if the query has no valid action. */
export function parseConnectionQuery(search: string): ConnectionIntent | null {
  const query = new URLSearchParams(search);
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

/** True only for an `https` address. The page goes to a sign-in link only if this is true. */
export function isSafeRedirect(url: string | null): url is string {
  if (url === null) return false;
  try {
    return new URL(url).protocol === "https:";
  } catch {
    return false;
  }
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
  const read = connector.tools.filter((t) => t.access === "read").length;
  const write = connector.tools.length - read;
  const parts = [`${read} read`];
  if (write > 0) parts.push(`${write} write (asks first)`);
  return parts.join(", ");
}

/** The query of a page address without the entry-link parameters. */
export function withoutEntryParams(search: string): string {
  const query = new URLSearchParams(search);
  for (const key of ["workspace", "connect", "scope", "connection"]) query.delete(key);
  const rest = query.toString();
  return rest === "" ? "" : `?${rest}`;
}
