"use client";

// The Connections page (docs/architecture.md, Section 10.3). The user connects the tools of a
// workspace here. Secrets go only into the forms of this page, never into a chat.

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  type Connection,
  type ConnectionScope,
  type Connector,
  type McpAuth,
  type StartedConnection,
  errorText,
} from "@/lib/api";
import {
  type ConnectionList,
  EMPTY_SERVER,
  type ServerDraft,
  connectionOf,
  canCheck,
  connectorFor,
  customServers,
  hasOAuthAnswer,
  formProblem,
  formValues,
  initialValues,
  isSafeRedirect,
  listFor,
  mcpToolSummary,
  parseConnectionQuery,
  parsePatterns,
  patternProblem,
  serverBody,
  serverProblem,
  stateLabel,
  stateTone,
  toolSummary,
  withConnection,
  withoutConnection,
  withoutEntryParams,
} from "@/lib/connections";
import { toneBadge } from "@/lib/history";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Connections.module.css";

const SCOPE_LABELS: Record<ConnectionScope, string> = {
  personal: "Only you",
  workspace: "Whole workspace",
};

const AUTH_LABELS: Record<McpAuth, string> = {
  oauth: "Sign in (OAuth)",
  bearer: "Access token",
  header: "Key in a header",
  none: "No sign-in",
};

const ACCESS_LABELS = { read: "Read", write: "Write (asks first)", destructive: "Can delete (asks first)" };

type Notice = { tone: "success" | "info" | "error"; connector: string | null; text: string } | null;

/** The line of a notice. The connector title goes first: "Gmail is connected." or "Gmail: Failed. …". */
function noticeText(notice: NonNullable<Notice>, catalog: Connector[] | null): string {
  if (notice.connector === null) return notice.text;
  const title = catalog?.find((c) => c.name === notice.connector)?.title ?? notice.connector;
  return notice.tone === "success" ? `${title} ${notice.text}` : `${title}: ${notice.text}`;
}

export function Connections() {
  const api = useApi();
  const { me, meError, reloadMe, workspaces, workspaceId, setWorkspaceId } = useData();
  const [notice, setNotice] = useState<Notice>(null);
  // The connection whose form is open (an `http_api` connector).
  const [formFor, setFormFor] = useState<string | null>(null);
  const handled = useRef(false);

  const loadCatalog = useCallback(() => api.listConnectors(), [api]);
  const catalog = useLoad(loadCatalog, "Could not load the connectors.");

  // The connections of the workspace. The list keeps its workspace, so the page never shows
  // (or acts on) the rows of the workspace before. `changes` counts the changes of the page: a
  // list that started to load before a change is old, so the page loads it again.
  const [list, setList] = useState<ConnectionList | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const changes = useRef(0);
  useEffect(() => {
    if (workspaceId === null) return;
    let live = true;
    const seen = changes.current;
    api.listConnections(workspaceId).then(
      (items) => {
        if (!live) return;
        if (changes.current !== seen) {
          setTick((n) => n + 1);
          return;
        }
        setList({ workspaceId, items });
        setListError(null);
      },
      (e: unknown) => {
        if (live) setListError(errorText(e, "Could not load the connections."));
      },
    );
    return () => {
      live = false;
    };
  }, [api, workspaceId, tick]);
  const reloadList = useCallback(() => {
    setListError(null);
    setTick((n) => n + 1);
  }, []);

  const replace = useCallback((saved: Connection) => {
    changes.current += 1;
    setList((current) => withConnection(current, saved));
  }, []);
  const remove = useCallback((id: string) => {
    changes.current += 1;
    setList((current) => withoutConnection(current, id));
  }, []);

  /** After a start: go to the sign-in link, or open the form if the connection needs one. */
  const opened = useCallback(
    (started: StartedConnection) => {
      if (isSafeRedirect(started.redirect_url)) {
        window.location.assign(started.redirect_url);
        return;
      }
      replace(started.connection);
      if (started.connection.state === "pending") setFormFor(started.connection.id);
    },
    [replace],
  );

  const start = useCallback(
    async (workspace: string, connector: string, scope: ConnectionScope) => {
      opened(await api.startConnection(workspace, connector, scope));
    },
    [api, opened],
  );

  // The entry links: `?connect=` starts a connection, `?connection=` checks it after sign-in.
  useEffect(() => {
    if (handled.current || me === null) return;
    handled.current = true;
    const search = window.location.search;
    const intent = parseConnectionQuery(search);
    const answer = hasOAuthAnswer(search);
    if (intent === null && !answer) return;
    // Remove the entry link and any OAuth answer from the address, also when it is not valid.
    history.replaceState(null, "", window.location.pathname + withoutEntryParams(search));
    // The state changes run after the effect, in the promise.
    const run = async () => {
      if (intent === null) throw new Error("The sign-in answer is not valid. Connect again.");
      if (intent.kind === "oauth") {
        const done = await api.finishOAuth(intent.state, intent.code, intent.error);
        if (workspaces.some((w) => w.id === done.workspace_id)) setWorkspaceId(done.workspace_id);
        replace(done);
        setNotice(
          done.state === "connected"
            ? { tone: "success", connector: done.connector, text: "is connected." }
            : { tone: "error", connector: done.connector, text: `${stateLabel(done.state)}. ${done.last_error ?? ""}`.trim() },
        );
        return;
      }
      if (!workspaces.some((w) => w.id === intent.workspaceId)) {
        throw new Error("You are not in the workspace of this link.");
      }
      setWorkspaceId(intent.workspaceId);
      if (intent.kind === "connect") {
        await start(intent.workspaceId, intent.connector, intent.scope);
        return;
      }
      const checked = await api.checkConnection(intent.connectionId);
      if (checked.workspace_id !== intent.workspaceId) {
        throw new Error("The connection is not in the workspace of this link.");
      }
      replace(checked);
      const tone = stateTone(checked.state);
      setNotice({
        tone: tone === "ok" ? "success" : tone === "error" ? "error" : "info",
        connector: checked.connector,
        text:
          checked.state === "connected"
            ? "is connected."
            : `${stateLabel(checked.state)}. ${checked.last_error ?? ""}`.trim(),
      });
    };
    Promise.resolve()
      .then(run)
      .catch((e: unknown) =>
        setNotice({ tone: "error", connector: null, text: errorText(e, "The connection did not start.") }),
      );
  }, [api, me, workspaces, setWorkspaceId, start, replace]);

  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the connections" />;
  const workspace = workspaces.find((w) => w.id === workspaceId) ?? null;
  const rows = workspace === null ? null : listFor(list, workspace.id);
  const cards = catalog.data === null || rows === null ? [] : [...catalog.data, ...customServers(rows, catalog.data)];

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Link href="/" className={styles.back}>
          Back to sessions
        </Link>
        <h1 className={styles.title}>Connections</h1>
        <p className={styles.meta}>
          {workspace ? workspace.label : "No workspace"} · Connect the tools that the agent can
          use. Give passwords and keys only on this page, never in a chat.
        </p>
      </header>

      {notice ? (
        <p
          className={`alert ${notice.tone === "success" ? "alert-success" : notice.tone === "error" ? "alert-error" : ""}`}
          role="status"
        >
          {noticeText(notice, catalog.data)}
        </p>
      ) : null}

      {workspace === null ? (
        <p className="alert">You are not in a workspace yet. Ask an admin of your org to add you.</p>
      ) : catalog.error ? (
        <ErrorState title="The connectors did not load" message={catalog.error} onRetry={catalog.reload} />
      ) : listError ? (
        <ErrorState title="The connections did not load" message={listError} onRetry={reloadList} />
      ) : catalog.data === null || rows === null ? (
        <LoadingState label="Loading the connections" />
      ) : (
        <>
        {cards.length === 0 ? <p className="alert">This sama has no connectors.</p> : null}
        <ul className={styles.list}>
          {cards.map((connector) => (
            <li key={connector.name}>
              <ConnectorCard
                connector={connector}
                connections={rows}
                formFor={formFor}
                onStart={(scope) => start(workspace.id, connector.name, scope)}
                onSaved={(saved) => {
                  replace(saved);
                  if (saved.state === "connected") setFormFor(null);
                }}
                onCloseForm={() => setFormFor(null)}
                onRemoved={remove}
              />
            </li>
          ))}
        </ul>
        <AddServer
          onAdd={async (draft) => {
            opened(await api.addMcpServer(workspace.id, serverBody(draft)));
          }}
        />
        </>
      )}
    </div>
  );
}

/** The form to add a remote MCP server by URL. Only a workspace admin can send it. */
function AddServer({ onAdd }: { onAdd: (draft: ServerDraft) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<ServerDraft>(EMPTY_SERVER);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const edit = (patch: Partial<ServerDraft>) => setDraft((d) => ({ ...d, ...patch }));

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const problem = serverProblem(draft);
    if (problem !== null) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onAdd(draft);
      setDraft(EMPTY_SERVER);
      setOpen(false);
    } catch (e) {
      setError(errorText(e, "The server was not added."));
    } finally {
      setSaving(false);
    }
  };

  if (!open) {
    return (
      <div>
        <button type="button" className="btn btn-small" onClick={() => setOpen(true)}>
          Add a server by URL
        </button>
      </div>
    );
  }
  return (
    <section className={`card ${styles.card}`} aria-label="Add a remote MCP server">
      <div className={styles.cardHead}>
        <h2 className={styles.heading}>Add a remote MCP server</h2>
        <span className={styles.meta}>Only a workspace admin can add a server.</span>
      </div>
      <form className={styles.form} onSubmit={submit}>
        <label className="field">
          <span className="label">Name</span>
          <input
            className="input"
            value={draft.name}
            maxLength={40}
            autoComplete="off"
            onChange={(e) => edit({ name: e.target.value })}
          />
          <span className="hint">The tools get the names mcp_&lt;name&gt;_&lt;tool&gt;.</span>
        </label>
        <label className="field">
          <span className="label">Title (optional)</span>
          <input className="input" value={draft.title} maxLength={60} onChange={(e) => edit({ title: e.target.value })} />
        </label>
        <label className="field">
          <span className="label">Address</span>
          <input
            className="input"
            type="url"
            inputMode="url"
            placeholder="https://mcp.example.com/mcp"
            value={draft.url}
            maxLength={2000}
            autoComplete="off"
            onChange={(e) => edit({ url: e.target.value })}
          />
        </label>
        <label className="field">
          <span className="label">Sign-in</span>
          <select className="input" value={draft.auth} onChange={(e) => edit({ auth: e.target.value as McpAuth })}>
            {(Object.keys(AUTH_LABELS) as McpAuth[]).map((auth) => (
              <option key={auth} value={auth}>
                {AUTH_LABELS[auth]}
              </option>
            ))}
          </select>
          <span className="hint">You give a token or a key in the next step, not here.</span>
        </label>
        {draft.auth === "header" ? (
          <label className="field">
            <span className="label">Header name</span>
            <input
              className="input"
              placeholder="X-Api-Key"
              value={draft.header}
              maxLength={64}
              autoComplete="off"
              onChange={(e) => edit({ header: e.target.value })}
            />
          </label>
        ) : null}
        {draft.auth === "oauth" ? (
          <label className="field">
            <span className="label">OAuth scope (optional)</span>
            <input
              className="input"
              value={draft.oauthScope}
              maxLength={400}
              autoComplete="off"
              onChange={(e) => edit({ oauthScope: e.target.value })}
            />
          </label>
        ) : null}
        <label className="field">
          <span className="label">Who can use it</span>
          <select className="input" value={draft.scope} onChange={(e) => edit({ scope: e.target.value as ConnectionScope })}>
            {(Object.keys(SCOPE_LABELS) as ConnectionScope[]).map((scope) => (
              <option key={scope} value={scope}>
                {SCOPE_LABELS[scope]}
              </option>
            ))}
          </select>
        </label>
        {error ? (
          <p className="alert alert-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className={styles.actions}>
          <button type="submit" className="btn btn-primary btn-small" disabled={saving}>
            {saving ? "Adding" : "Add and connect"}
          </button>
          <button
            type="button"
            className="btn btn-quiet btn-small"
            disabled={saving}
            onClick={() => {
              setOpen(false);
              setError(null);
            }}
          >
            Cancel
          </button>
        </div>
      </form>
    </section>
  );
}

function ConnectorCard({
  connector,
  connections,
  formFor,
  onStart,
  onSaved,
  onCloseForm,
  onRemoved,
}: {
  connector: Connector;
  connections: Connection[];
  formFor: string | null;
  onStart: (scope: ConnectionScope) => Promise<void>;
  onSaved: (connection: Connection) => void;
  onCloseForm: () => void;
  onRemoved: (id: string) => void;
}) {
  return (
    <section className={`card ${styles.card}`} aria-label={connector.title}>
      <div className={styles.cardHead}>
        <h2 className={styles.heading}>{connector.title}</h2>
        <span className={styles.meta}>{toolSummary(connector)}</span>
      </div>
      <p className={styles.description}>{connector.description}</p>
      {connector.scopes.map((scope) => (
        <ScopeRow
          key={scope}
          connector={connector}
          scope={scope}
          connection={connectionOf(connections, connector.name, scope)}
          formFor={formFor}
          onStart={() => onStart(scope)}
          onSaved={onSaved}
          onCloseForm={onCloseForm}
          onRemoved={onRemoved}
        />
      ))}
    </section>
  );
}

function ScopeRow({
  connector,
  scope,
  connection,
  formFor,
  onStart,
  onSaved,
  onCloseForm,
  onRemoved,
}: {
  connector: Connector;
  scope: ConnectionScope;
  connection: Connection | null;
  formFor: string | null;
  onStart: () => Promise<void>;
  onSaved: (connection: Connection) => void;
  onCloseForm: () => void;
  onRemoved: (id: string) => void;
}) {
  const api = useApi();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const act = async (label: string, action: () => Promise<void>) => {
    setBusy(label);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(errorText(e, "The action failed."));
    } finally {
      setBusy(null);
    }
  };

  const connected = connection?.state === "connected";
  const showForm = connection !== null && connector.kind !== "composio" && connection.id === formFor;
  const tone = connection ? stateTone(connection.state) : "off";

  return (
    <div className={styles.scope}>
      <div className={styles.row}>
        <div className={styles.scopeName}>
          <span>{SCOPE_LABELS[scope]}</span>
          <span className={toneBadge(tone)}>{connection ? stateLabel(connection.state) : "Not connected"}</span>
        </div>
        <div className={styles.actions}>
          {!connected ? (
            <button
              type="button"
              className="btn btn-primary btn-small"
              disabled={busy !== null}
              onClick={() => act("connect", onStart)}
            >
              {busy === "connect" ? "Connecting" : connection === null ? "Connect" : "Connect again"}
            </button>
          ) : null}
          {connection !== null && canCheck(connector, connection) ? (
            <button
              type="button"
              className="btn btn-small"
              disabled={busy !== null}
              onClick={() => act("check", async () => onSaved(await api.checkConnection(connection.id)))}
            >
              {busy === "check" ? "Checking" : "Check"}
            </button>
          ) : null}
          {connected ? (
            <button
              type="button"
              className="btn btn-quiet btn-small"
              disabled={busy !== null}
              onClick={() => act("disable", async () => onSaved(await api.disableConnection(connection.id)))}
            >
              {busy === "disable" ? "Disabling" : "Disable"}
            </button>
          ) : null}
          {connection !== null ? (
            <button
              type="button"
              className="btn btn-danger btn-small"
              disabled={busy !== null}
              onClick={() => {
                if (!window.confirm(`Remove the ${connector.title} connection? The agent can no longer use it.`)) return;
                void act("remove", async () => {
                  await api.deleteConnection(connection.id);
                  onRemoved(connection.id);
                });
              }}
            >
              {busy === "remove" ? "Removing" : "Remove"}
            </button>
          ) : null}
        </div>
      </div>
      {connection?.last_error ? <p className={styles.error}>{connection.last_error}</p> : null}
      {connection?.mcp ? (
        <McpTools
          // A new key when the saved filters change, so the editor shows the filters of another admin.
          key={`${connection.id}:${connection.mcp.include.join("\n")}|${connection.mcp.exclude.join("\n")}`}
          connection={connection}
          onSaved={onSaved}
        />
      ) : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      {showForm ? (
        <ConnectForm
          connector={connectorFor(connector, connection)}
          connection={connection}
          onSaved={onSaved}
          onCancel={onCloseForm}
        />
      ) : null}
    </div>
  );
}

function ConnectForm({
  connector,
  connection,
  onSaved,
  onCancel,
}: {
  connector: Connector;
  connection: Connection;
  onSaved: (connection: Connection) => void;
  onCancel: () => void;
}) {
  const api = useApi();
  const [values, setValues] = useState(() => initialValues(connector, connection));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problem = formProblem(connector.fields, values);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (problem !== null) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const saved = await api.submitConnectionForm(connection.id, formValues(connector.fields, values));
      // The secret leaves the page state as soon as the API has it.
      setValues(initialValues(connector, saved));
      onSaved(saved);
      // The row shows `last_error` already; the form says only that the test failed.
      if (saved.state !== "connected") setError("The test failed. Check the values and try again.");
    } catch (e) {
      setError(errorText(e, "The form was not saved."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className={styles.form} onSubmit={submit} aria-label={`Connect ${connector.title}`}>
      {connector.fields.map((field) => (
        <label key={field.name} className="field">
          <span className="label">
            {field.label}
            {field.required ? "" : " (optional)"}
          </span>
          <input
            className="input"
            type={field.secret ? "password" : "text"}
            autoComplete={field.secret ? "new-password" : "off"}
            value={values[field.name] ?? ""}
            onChange={(e) => setValues((v) => ({ ...v, [field.name]: e.target.value }))}
          />
          {field.help ? <span className="hint">{field.help}</span> : null}
        </label>
      ))}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button type="submit" className="btn btn-primary btn-small" disabled={saving}>
          {saving ? "Testing" : "Save and test"}
        </button>
        <button type="button" className="btn btn-quiet btn-small" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/** The tools of a remote MCP server, and the filters that turn tools on and off. */
function McpTools({ connection, onSaved }: { connection: Connection; onSaved: (connection: Connection) => void }) {
  const api = useApi();
  const mcp = connection.mcp;
  const [include, setInclude] = useState(() => (mcp?.include ?? []).join("\n"));
  const [exclude, setExclude] = useState(() => (mcp?.exclude ?? []).join("\n"));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const summary = mcpToolSummary(connection);
  if (!mcp || summary === null) return null;

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    const wanted = parsePatterns(include);
    const unwanted = parsePatterns(exclude);
    const problem = patternProblem(wanted) ?? patternProblem(unwanted);
    if (problem !== null) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      onSaved(await api.setConnectionTools(connection.id, wanted, unwanted));
    } catch (e) {
      setError(errorText(e, "The filters were not saved."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <details className={styles.tools}>
      <summary className={styles.meta}>Tools: {summary}</summary>
      <ul className={styles.toolList}>
        {mcp.tools.map((tool) => (
          <li key={tool.name} className={tool.enabled ? styles.tool : `${styles.tool} ${styles.toolOff}`}>
            <span className={styles.toolName}>{tool.remote_name}</span>
            <span className="badge">{tool.enabled ? ACCESS_LABELS[tool.access] : "Off"}</span>
            {tool.description ? <span className={styles.toolText}>{tool.description}</span> : null}
          </li>
        ))}
      </ul>
      <form className={styles.form} onSubmit={save} aria-label="Tool filters">
        <label className="field">
          <span className="label">Use only these tools (optional)</span>
          <textarea className="input" rows={2} value={include} onChange={(e) => setInclude(e.target.value)} />
          <span className="hint">One name or pattern on each line, for example list_*. Empty: all tools.</span>
        </label>
        <label className="field">
          <span className="label">Never use these tools (optional)</span>
          <textarea className="input" rows={2} value={exclude} onChange={(e) => setExclude(e.target.value)} />
        </label>
        {error ? (
          <p className="alert alert-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className={styles.actions}>
          <button type="submit" className="btn btn-small" disabled={saving}>
            {saving ? "Saving" : "Save filters"}
          </button>
        </div>
      </form>
    </details>
  );
}
