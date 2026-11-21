"use client";

// The MCP servers section: the remote MCP servers and the stdio MCP servers (in a connector host)
// of the workspace. A list view, a detail view of one server (state, scope, tools and their
// filters, test, disable or enable, delete), and a form to add a server.

import { useCallback, useRef, useState } from "react";
import { type Connection, errorText, type HostedCustom } from "@/lib/api";
import {
  connectorFor,
  customServers,
  isSafeRedirect,
  mcpToolSummary,
  serverBody,
  stateLabel,
  stateTone,
  stdioBody,
} from "@/lib/connections";
import { can } from "@/lib/console";
import { toneBadge } from "@/lib/history";
import {
  canChangeServer,
  canDisableServer,
  canEnableServer,
  enableNotice,
  mcpServers,
  type ServerNotice,
  SCOPE_TEXT,
  serverAddress,
  serverError,
  serverKindLabel,
  serverTitle,
  testNotice,
} from "@/lib/mcpServers";
import { relativeTime } from "@/lib/time";
import { useApi } from "../../AuthProvider";
import { AddServer, AddStdio, ConnectForm, HostedInfo, McpTools } from "../../Connections";
import { ErrorState, LoadingState } from "../../ui";
import { useFocusWhen } from "../../useFocusWhen";
import { useLoad } from "../../useLoad";
import { useConsole } from "../Console";
import { SectionHeader } from "../parts";
import styles from "../../Page.module.css";
import own from "./McpServers.module.css";

type View = { kind: "list" } | { kind: "new" } | { kind: "detail"; id: string };
type AddKind = "remote" | "stdio";

export function McpServersSection() {
  const api = useApi();
  const { org, workspaceId } = useConsole();
  const [view, setView] = useState<View>({ kind: "list" });
  const [notice, setNotice] = useState<string | null>(null);
  const load = useCallback(() => api.listConnections(workspaceId), [api, workspaceId]);
  const connections = useLoad(load, "Could not load the MCP servers.");
  // The API lets a workspace admin or the org owner add a server; the web knows only the org role.
  const canAdd = can(org, "connections.add") && org.admin;

  const rows = connections.data === null ? null : mcpServers(connections.data);
  const replace = (saved: Connection) =>
    connections.setData((all) =>
      all === null ? all : all.some((c) => c.id === saved.id) ? all.map((c) => (c.id === saved.id ? saved : c)) : [...all, saved],
    );
  const remove = (id: string) => connections.setData((all) => (all === null ? all : all.filter((c) => c.id !== id)));

  const detail = view.kind === "detail" ? (rows?.find((c) => c.id === view.id) ?? null) : null;

  return (
    <>
      <SectionHeader
        id="mcp"
        actions={
          view.kind === "list" && canAdd ? (
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                setNotice(null);
                setView({ kind: "new" });
              }}
            >
              Add an MCP server
            </button>
          ) : null
        }
        meta={canAdd ? undefined : "A workspace admin or the org owner adds MCP servers. An org owner can take this right away from a role."}
      />
      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      {connections.error ? (
        <ErrorState title="The MCP servers did not load" message={connections.error} onRetry={connections.reload} />
      ) : rows === null ? (
        <LoadingState label="Loading the MCP servers" />
      ) : view.kind === "new" ? (
        <NewServer
          workspaceId={workspaceId}
          onCancel={() => setView({ kind: "list" })}
          onAdded={(saved, text) => {
            replace(saved);
            setNotice(text);
            setView({ kind: "detail", id: saved.id });
          }}
        />
      ) : view.kind === "detail" && detail !== null ? (
        <ServerDetail
          key={detail.id}
          connection={detail}
          onBack={() => setView({ kind: "list" })}
          onChanged={replace}
          onRemoved={(title) => {
            remove(detail.id);
            setNotice(`The server ${title} is deleted.`);
            setView({ kind: "list" });
          }}
        />
      ) : (
        <ServerList
          rows={rows}
          canAdd={canAdd}
          onNew={() => {
            setNotice(null);
            setView({ kind: "new" });
          }}
          onOpen={(id) => {
            setNotice(null);
            setView({ kind: "detail", id });
          }}
        />
      )}
    </>
  );
}

function ServerList({
  rows,
  canAdd,
  onNew,
  onOpen,
}: {
  rows: Connection[];
  canAdd: boolean;
  onNew: () => void;
  onOpen: (id: string) => void;
}) {
  if (rows.length === 0) {
    return (
      <div className="state">
        <h2>No MCP servers yet</h2>
        <p>
          {canAdd
            ? "Add an MCP server to give the agents of this workspace more tools."
            : "Nobody added an MCP server to this workspace."}
        </p>
        {canAdd ? (
          <button type="button" className="btn btn-primary" onClick={onNew}>
            Add an MCP server
          </button>
        ) : null}
      </div>
    );
  }
  return (
    <ul className={styles.list} aria-label="MCP servers">
      {rows.map((c) => {
        const address = serverAddress(c);
        const tools = mcpToolSummary(c);
        return (
          <li key={c.id}>
            <section className={`card ${styles.card}`} aria-label={serverTitle(c)}>
              <div className={styles.cardHead}>
                <h2 className={styles.heading}>{serverTitle(c)}</h2>
                <div className={styles.name}>
                  <span className={toneBadge(stateTone(c.state))}>{stateLabel(c.state)}</span>
                  <span className="badge">{serverKindLabel(c)}</span>
                  <span className="badge">{SCOPE_TEXT[c.scope]}</span>
                </div>
              </div>
              {address ? <p className={styles.text}>{address}</p> : null}
              <p className={styles.meta}>{tools ?? "No tools found yet."}</p>
              {c.last_error ? <p className={styles.error}>{serverError(c.last_error)}</p> : null}
              <div className={styles.actions}>
                <button type="button" className="btn btn-small" onClick={() => onOpen(c.id)}>
                  Open
                </button>
              </div>
            </section>
          </li>
        );
      })}
    </ul>
  );
}

function NewServer({
  workspaceId,
  onCancel,
  onAdded,
}: {
  workspaceId: string;
  onCancel: () => void;
  onAdded: (connection: Connection, notice: string) => void;
}) {
  const api = useApi();
  const [kind, setKind] = useState<AddKind>("remote");

  // After an add: go to the sign-in link when the server needs one, else open its detail.
  const added = (started: { connection: Connection; redirect_url: string | null }) => {
    if (isSafeRedirect(started.redirect_url)) {
      window.location.assign(started.redirect_url);
      return;
    }
    const state = started.connection.state;
    onAdded(
      started.connection,
      state === "connected"
        ? `The server ${serverTitle(started.connection)} is added and connected.`
        : `The server ${serverTitle(started.connection)} is added. Its state is: ${stateLabel(state)}.`,
    );
  };

  return (
    <>
      <div className={styles.actions}>
        <button type="button" className="btn btn-quiet btn-small" onClick={onCancel}>
          Back to the servers
        </button>
      </div>
      <div className={styles.actions} role="group" aria-label="Type of server">
        <button
          type="button"
          className={`btn btn-small ${kind === "remote" ? "btn-primary" : ""}`}
          aria-pressed={kind === "remote"}
          onClick={() => setKind("remote")}
        >
          Remote server by URL
        </button>
        <button
          type="button"
          className={`btn btn-small ${kind === "stdio" ? "btn-primary" : ""}`}
          aria-pressed={kind === "stdio"}
          onClick={() => setKind("stdio")}
        >
          Stdio server in a connector host
        </button>
      </div>
      {kind === "remote" ? (
        <AddServer
          key="remote"
          startOpen
          onClose={onCancel}
          onAdd={async (draft) => added(await api.addMcpServer(workspaceId, serverBody(draft)))}
        />
      ) : (
        <AddStdio
          key="stdio"
          startOpen
          onClose={onCancel}
          onAdd={async (draft) => added(await api.addStdioServer(workspaceId, stdioBody(draft)))}
        />
      )}
    </>
  );
}

type Busy = "check" | "disable" | "enable" | "delete";

function ServerDetail({
  connection,
  onBack,
  onChanged,
  onRemoved,
}: {
  connection: Connection;
  onBack: () => void;
  onChanged: (connection: Connection) => void;
  onRemoved: (title: string) => void;
}) {
  const api = useApi();
  const { org } = useConsole();
  const [busy, setBusy] = useState<Busy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<ServerNotice | null>(null);
  const [now] = useState(() => Date.now());
  const heading = useRef<HTMLHeadingElement>(null);
  // The focus goes to the title when the detail opens.
  useFocusWhen(true, heading);

  const title = serverTitle(connection);
  const address = serverAddress(connection);
  const tools = mcpToolSummary(connection);
  const changeable = canChangeServer(org, connection);
  const custom = connection.mcp?.custom ?? null;
  // The form of a server that waits for a token. It needs the custom part of the server.
  const form =
    connection.state === "pending" && custom !== null
      ? connectorFor(customServers([connection], [])[0]!, connection)
      : null;

  const run = async (label: Busy, action: () => Promise<void>, fallback: string) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await action();
    } catch (e) {
      setError(errorText(e, fallback));
    } finally {
      setBusy(null);
    }
  };

  const check = () =>
    run(
      "check",
      async () => {
        const saved = await api.checkConnection(connection.id);
        onChanged(saved);
        setNotice(testNotice(saved));
      },
      "The test did not run.",
    );
  const disable = () =>
    run(
      "disable",
      async () => {
        onChanged(await api.disableConnection(connection.id));
        setNotice({ tone: "success", text: "The server is disabled. The agents cannot use its tools." });
      },
      "The server was not disabled.",
    );
  const enable = () =>
    run(
      "enable",
      async () => {
        const saved = await api.enableConnection(connection.id);
        onChanged(saved);
        setNotice(enableNotice(saved));
      },
      "The server was not enabled.",
    );
  const remove = () => {
    if (!window.confirm(`Delete the server ${title}? The agents can no longer use it.`)) return;
    void run(
      "delete",
      async () => {
        await api.deleteConnection(connection.id);
        onRemoved(title);
      },
      "The server was not deleted.",
    );
  };

  return (
    <>
      <div className={styles.actions}>
        <button type="button" className="btn btn-quiet btn-small" onClick={onBack}>
          Back to the servers
        </button>
      </div>
      <section className={`card ${styles.card}`} aria-labelledby="mcp-detail">
        <div className={styles.cardHead}>
          <h2 id="mcp-detail" ref={heading} tabIndex={-1} className={styles.heading}>
            {title}
          </h2>
          <div className={styles.name}>
            <span className={toneBadge(stateTone(connection.state))}>{stateLabel(connection.state)}</span>
            <span className="badge">{serverKindLabel(connection)}</span>
          </div>
        </div>
        <dl className={own.details}>
          <dt>Name</dt>
          <dd>{connection.connector}</dd>
          {address ? (
            <>
              <dt>{connection.kind === "mcp_hosted" ? "What it runs" : "Address"}</dt>
              <dd>{address}</dd>
            </>
          ) : null}
          <dt>Who can use it</dt>
          <dd>{SCOPE_TEXT[connection.scope]}</dd>
          <dt>Last check</dt>
          <dd>{connection.last_health_at ? relativeTime(connection.last_health_at, now) : "Not checked yet"}</dd>
          <dt>Tools</dt>
          <dd>{tools ?? "No tools found yet. Test the connection to find them."}</dd>
        </dl>
        {connection.kind === "mcp_hosted" && custom !== null ? <HostedInfo hosted={custom as HostedCustom} /> : null}
        {connection.last_error ? (
          <p className="alert alert-error" role="alert">
            Last error: {serverError(connection.last_error)}
          </p>
        ) : null}
        {changeable ? (
          <div className={styles.actions}>
            <button type="button" className="btn btn-small" disabled={busy !== null} onClick={() => void check()}>
              {busy === "check" ? "Testing…" : "Test connection"}
            </button>
            {canDisableServer(connection) ? (
              <button type="button" className="btn btn-quiet btn-small" disabled={busy !== null} onClick={() => void disable()}>
                {busy === "disable" ? "Disabling…" : "Disable"}
              </button>
            ) : null}
            {canEnableServer(connection) ? (
              <button type="button" className="btn btn-primary btn-small" disabled={busy !== null} onClick={() => void enable()}>
                {busy === "enable" ? "Enabling…" : "Enable"}
              </button>
            ) : null}
            <button type="button" className="btn btn-danger btn-small" disabled={busy !== null} onClick={remove}>
              {busy === "delete" ? "Deleting…" : "Delete"}
            </button>
          </div>
        ) : (
          <p className={styles.meta}>
            Only the owner, a workspace admin, or the org owner can change this server.
          </p>
        )}
        {notice ? (
          <p
            className={`alert ${notice.tone === "success" ? "alert-success" : notice.tone === "error" ? "alert-error" : ""}`}
            role={notice.tone === "error" ? "alert" : "status"}
          >
            {notice.text}
          </p>
        ) : null}
        {error ? (
          <p className="alert alert-error" role="alert">
            {error}
          </p>
        ) : null}
      </section>
      {form !== null && form.fields.length > 0 && changeable ? (
        <section className={`card ${styles.card}`} aria-label={`Connect ${title}`}>
          <h2 className={styles.heading}>Finish the connection</h2>
          <p className={styles.text}>This server needs a secret. The secret goes only into this form.</p>
          <ConnectForm connector={form} connection={connection} onSaved={onChanged} onCancel={onBack} />
        </section>
      ) : null}
      {connection.mcp ? (
        <section className={`card ${styles.card}`} aria-labelledby="mcp-tools">
          <h2 id="mcp-tools" className={styles.heading}>
            Tools
          </h2>
          {changeable ? (
            <McpTools
              // A new key when the saved filters change, so the editor shows the filters of another admin.
              key={`${connection.id}:${connection.mcp.include.join("\n")}|${connection.mcp.exclude.join("\n")}`}
              connection={connection}
              onSaved={onChanged}
            />
          ) : null}
          {mcpToolSummary(connection) === null ? (
            <p className={styles.meta}>No tools found yet. Test the connection to find them.</p>
          ) : !changeable ? (
            <ul className={styles.entries}>
              {connection.mcp.tools.map((t) => (
                <li key={t.name} className={styles.entry}>
                  {t.remote_name} {t.enabled ? "" : "(off)"}
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}
    </>
  );
}
