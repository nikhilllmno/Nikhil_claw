// The MCP servers section of the settings console (docs/architecture.md, Section 17.7, M7.10):
// the connections of a workspace that are MCP servers, and what the screen shows for each.
// The API checks every action again.

import type { Connection, ConnectionScope } from "./api";
import { hostedWhat, stateLabel, stateTone } from "./connections";

/** The kinds of connection that are MCP servers: a remote server and a stdio server in a connector host. */
export const MCP_KINDS: readonly string[] = ["mcp_remote", "mcp_hosted"];

export function isMcpServer(connection: Pick<Connection, "kind">): boolean {
  return MCP_KINDS.includes(connection.kind);
}

/** The MCP servers of a list of connections, sorted by title. */
export function mcpServers(connections: readonly Connection[]): Connection[] {
  return connections.filter(isMcpServer).sort((a, b) => serverTitle(a).localeCompare(serverTitle(b)));
}

/** The title of a server: the title that the admin gave, else the name. */
export function serverTitle(connection: Pick<Connection, "connector" | "mcp">): string {
  return connection.mcp?.custom?.title || connection.connector;
}

export function serverKindLabel(connection: Pick<Connection, "kind">): string {
  return connection.kind === "mcp_hosted" ? "Stdio server" : "Remote server";
}

export const SCOPE_TEXT: Record<ConnectionScope, string> = {
  personal: "Only you",
  workspace: "Whole workspace",
};

/**
 * What a server is: the address of a remote server, or what a connector host runs. `null` for a
 * server of the catalog, which has no custom part.
 */
export function serverAddress(connection: Pick<Connection, "kind" | "mcp">): string | null {
  const custom = connection.mcp?.custom;
  if (!custom) return null;
  if (connection.kind === "mcp_hosted") {
    const hosted = custom as Parameters<typeof hostedWhat>[0];
    return hosted.package || hosted.command ? `Runs ${hostedWhat(hosted)}` : null;
  }
  return "url" in custom ? custom.url : null;
}

/**
 * True when the user can change the connection: an org admin, or the owner of a personal
 * connection. The API decides again (the owner, a workspace admin, or the org owner).
 */
export function canChangeServer(
  org: { admin: boolean; user_id: string },
  connection: Pick<Connection, "owner_user_id" | "scope">,
): boolean {
  if (org.admin) return true;
  return connection.scope === "personal" && connection.owner_user_id === org.user_id;
}

/** True for the Enable button: only a disabled server. */
export function canEnableServer(connection: Pick<Connection, "state">): boolean {
  return connection.state === "disabled";
}

/** True for the Disable button: any server that is not disabled already. */
export function canDisableServer(connection: Pick<Connection, "state">): boolean {
  return connection.state !== "disabled";
}

export interface ServerNotice {
  tone: "success" | "error" | "info";
  text: string;
}

/** The notice after a test of the connection. The state comes from the API. */
export function testNotice(connection: Pick<Connection, "state" | "last_error">): ServerNotice {
  const tone = stateTone(connection.state);
  if (tone === "ok") return { tone: "success", text: "The test worked. The server is connected." };
  const why = connection.last_error ? ` ${connection.last_error}` : "";
  if (tone === "wait") return { tone: "info", text: `The server is not ready. Its state is: ${stateLabel(connection.state)}.${why}` };
  return { tone: "error", text: `The test failed. Its state is: ${stateLabel(connection.state)}.${why}` };
}

/** The notice after an enable. */
export function enableNotice(connection: Pick<Connection, "state" | "last_error">): ServerNotice {
  const tone = stateTone(connection.state);
  if (tone === "ok") return { tone: "success", text: "The server is enabled and connected." };
  const why = connection.last_error ? ` ${connection.last_error}` : "";
  return { tone: tone === "off" ? "info" : "error", text: `The server is enabled. Its state is: ${stateLabel(connection.state)}.${why}` };
}
