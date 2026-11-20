"use client";

// The Connections page (docs/architecture.md, Section 10.3). The user connects the tools of a
// workspace here. Secrets go only into the forms of this page, never into a chat.

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  type ApiAuthType,
  type Connection,
  type ConnectionScope,
  type Connector,
  type HostedCustom,
  type McpAuth,
  type StartedConnection,
  errorText,
} from "@/lib/api";
import {
  type ApiDraft,
  type ConnectionList,
  EMPTY_API,
  EMPTY_SERVER,
  EMPTY_STDIO,
  type ServerDraft,
  type StdioDraft,
  apiBody,
  apiProblem,
  connectionOf,
  canCheck,
  connectorFor,
  customServers,
  hasOAuthAnswer,
  formProblem,
  formValues,
  hostedWhat,
  initialValues,
  isSafeRedirect,
  listFor,
  mcpToolSummary,
  parseConnectionQuery,
  parsePatterns,
  patternProblem,
  serverBody,
  serverProblem,
  stdioBody,
  stdioProblem,
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

const API_AUTH_LABELS: Record<ApiAuthType, string> = {
  api_key: "API key",
  bearer: "Bearer token",
  basic: "User name and password",
  oauth2: "OAuth 2 client credentials",
  none: "No sign-in",
};

const STDIO_SOURCES: Record<StdioDraft["source"], string> = {
  npm: "npm package",
  pypi: "PyPI package",
  command: "Custom command (org owner only)",
};

const API_SOURCES: Record<ApiDraft["source"], string> = {
  openapi: "Paste an OpenAPI document",
  openapi_url: "OpenAPI document at an address",
  endpoints: "List of endpoints",
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
        <div className={styles.actions}>
          <AddServer
            onAdd={async (draft) => {
              opened(await api.addMcpServer(workspace.id, serverBody(draft)));
            }}
          />
          <AddStdio
            onAdd={async (draft) => {
              opened(await api.addStdioServer(workspace.id, stdioBody(draft)));
            }}
          />
          <AddApi
            onAdd={async (draft) => {
              opened(await api.addApi(workspace.id, apiBody(draft)));
            }}
          />
        </div>
        </>
      )}
    </div>
  );
}

/**
 * The state of an add form: the draft, the open flag, and the submit that checks the draft
 * first. The form closes and resets after a good add.
 */
function useAddForm<T>(
  empty: T,
  problemOf: (draft: T) => string | null,
  onAdd: (draft: T) => Promise<void>,
  failed: string,
) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<T>(empty);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const edit = (patch: Partial<T>) => setDraft((d) => ({ ...d, ...patch }));
  const close = () => {
    setOpen(false);
    setError(null);
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const problem = problemOf(draft);
    if (problem !== null) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onAdd(draft);
      setDraft(empty);
      setOpen(false);
    } catch (e) {
      setError(errorText(e, failed));
    } finally {
      setSaving(false);
    }
  };
  return { open, setOpen, draft, edit, saving, error, close, submit };
}

/** The frame of an add form: the card, the heading, the error, and the buttons. */
function AddCard({
  title,
  note,
  error,
  saving,
  savingLabel = "Adding",
  onSubmit,
  onCancel,
  children,
}: {
  title: string;
  note: string;
  error: string | null;
  saving: boolean;
  savingLabel?: string;
  onSubmit: (event: React.FormEvent) => void;
  onCancel: () => void;
  children: React.ReactNode;
}) {
  return (
    <section className={`card ${styles.card} ${styles.addCard}`} aria-label={title}>
      <div className={styles.cardHead}>
        <h2 className={styles.heading}>{title}</h2>
        <span className={styles.meta}>{note}</span>
      </div>
      <form className={styles.form} onSubmit={onSubmit}>
        {children}
        {error ? (
          <p className="alert alert-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className={styles.actions}>
          <button type="submit" className="btn btn-primary btn-small" disabled={saving}>
            {saving ? savingLabel : "Add and connect"}
          </button>
          <button type="button" className="btn btn-quiet btn-small" disabled={saving} onClick={onCancel}>
            Cancel
          </button>
        </div>
      </form>
    </section>
  );
}

function ScopeSelect({ value, onChange }: { value: ConnectionScope; onChange: (scope: ConnectionScope) => void }) {
  return (
    <label className="field">
      <span className="label">Who can use it</span>
      <select className="input" value={value} onChange={(e) => onChange(e.target.value as ConnectionScope)}>
        {(Object.keys(SCOPE_LABELS) as ConnectionScope[]).map((scope) => (
          <option key={scope} value={scope}>
            {SCOPE_LABELS[scope]}
          </option>
        ))}
      </select>
    </label>
  );
}

function NameFields({
  name,
  title,
  prefix,
  onChange,
}: {
  name: string;
  title: string;
  prefix: string;
  onChange: (patch: { name?: string; title?: string }) => void;
}) {
  return (
    <>
      <label className="field">
        <span className="label">Name</span>
        <input className="input" value={name} maxLength={40} autoComplete="off" onChange={(e) => onChange({ name: e.target.value })} />
        <span className="hint">
          The tools get the names {prefix}_&lt;name&gt;_&lt;tool&gt;.
        </span>
      </label>
      <label className="field">
        <span className="label">Title (optional)</span>
        <input className="input" value={title} maxLength={60} onChange={(e) => onChange({ title: e.target.value })} />
      </label>
    </>
  );
}

/** The form to add a remote MCP server by URL. Only a workspace admin can send it. */
function AddServer({ onAdd }: { onAdd: (draft: ServerDraft) => Promise<void> }) {
  const { open, setOpen, draft, edit, saving, error, close, submit } = useAddForm(
    EMPTY_SERVER,
    serverProblem,
    onAdd,
    "The server was not added.",
  );

  if (!open) {
    return (
      <button type="button" className="btn btn-small" onClick={() => setOpen(true)}>
        Add a server by URL
      </button>
    );
  }
  return (
    <AddCard
      title="Add a remote MCP server"
      note="Only a workspace admin can add a server."
      error={error}
      saving={saving}
      onSubmit={submit}
      onCancel={close}
    >
      <NameFields name={draft.name} title={draft.title} prefix="mcp" onChange={edit} />
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
      <ScopeSelect value={draft.scope} onChange={(scope) => edit({ scope })} />
    </AddCard>
  );
}

/**
 * The form to add a stdio MCP server. sama runs it in a connector host: its own sandbox with
 * an egress allowlist. A workspace admin can add a pinned package. Only the org owner can add a
 * custom command.
 */
function AddStdio({ onAdd }: { onAdd: (draft: StdioDraft) => Promise<void> }) {
  const { open, setOpen, draft, edit, saving, error, close, submit } = useAddForm(
    EMPTY_STDIO,
    stdioProblem,
    onAdd,
    "The server was not added.",
  );

  if (!open) {
    return (
      <button type="button" className="btn btn-small" onClick={() => setOpen(true)}>
        Add a stdio MCP server
      </button>
    );
  }
  return (
    <AddCard
      title="Add a stdio MCP server"
      note="sama runs the server in its own connector host. The first start can take some minutes."
      error={error}
      saving={saving}
      savingLabel="Installing and testing"
      onSubmit={submit}
      onCancel={close}
    >
      <NameFields name={draft.name} title={draft.title} prefix="mcp" onChange={edit} />
      <label className="field">
        <span className="label">What to run</span>
        <select
          className="input"
          value={draft.source}
          onChange={(e) => edit({ source: e.target.value as StdioDraft["source"] })}
        >
          {(Object.keys(STDIO_SOURCES) as StdioDraft["source"][]).map((source) => (
            <option key={source} value={source}>
              {STDIO_SOURCES[source]}
            </option>
          ))}
        </select>
      </label>
      {draft.source === "command" ? (
        <label className="field">
          <span className="label">Command</span>
          <textarea
            className="input"
            rows={3}
            placeholder={"npx\n-y\n@example/mcp-server@1.2.3"}
            value={draft.command}
            onChange={(e) => edit({ command: e.target.value })}
          />
          <span className="hint">The program on the first line, then one argument on each line.</span>
        </label>
      ) : (
        <>
          <label className="field">
            <span className="label">Package</span>
            <input
              className="input"
              placeholder={draft.source === "npm" ? "@modelcontextprotocol/server-everything" : "mcp-server-fetch"}
              value={draft.packageName}
              maxLength={214}
              autoComplete="off"
              onChange={(e) => edit({ packageName: e.target.value })}
            />
          </label>
          <label className="field">
            <span className="label">Version</span>
            <input
              className="input"
              placeholder="1.2.3"
              value={draft.version}
              maxLength={64}
              autoComplete="off"
              onChange={(e) => edit({ version: e.target.value })}
            />
            <span className="hint">One exact version. sama does not accept a range or latest.</span>
          </label>
          <label className="field">
            <span className="label">Program (optional)</span>
            <input
              className="input"
              value={draft.bin}
              maxLength={100}
              autoComplete="off"
              onChange={(e) => edit({ bin: e.target.value })}
            />
            <span className="hint">The program of the package to start, if it has more than one.</span>
          </label>
          <label className="field">
            <span className="label">Arguments (optional)</span>
            <textarea className="input" rows={2} value={draft.args} onChange={(e) => edit({ args: e.target.value })} />
            <span className="hint">One argument on each line.</span>
          </label>
        </>
      )}
      <label className="field">
        <span className="label">Environment variables (optional)</span>
        <textarea
          className="input"
          rows={2}
          placeholder="GITHUB_TOKEN"
          value={draft.env}
          onChange={(e) => edit({ env: e.target.value })}
        />
        <span className="hint">Only the names. You give the values in the next step, not here.</span>
      </label>
      <label className="field">
        <span className="label">Hosts that the server can reach (optional)</span>
        <textarea
          className="input"
          rows={2}
          placeholder="api.github.com"
          value={draft.egress}
          onChange={(e) => edit({ egress: e.target.value })}
        />
        <span className="hint">The host blocks all other addresses. sama adds the package registry.</span>
      </label>
      <label className="field">
        <span className="label">Memory limit in MB (optional)</span>
        <input className="input" inputMode="numeric" placeholder="384" value={draft.memoryMb} onChange={(e) => edit({ memoryMb: e.target.value })} />
      </label>
      <label className="field">
        <span className="label">Pause after idle seconds (optional)</span>
        <input className="input" inputMode="numeric" placeholder="600" value={draft.idleS} onChange={(e) => edit({ idleS: e.target.value })} />
      </label>
      <ScopeSelect value={draft.scope} onChange={(scope) => edit({ scope })} />
    </AddCard>
  );
}

/** The form to add an HTTP API from an OpenAPI document or a list of endpoints. */
function AddApi({ onAdd }: { onAdd: (draft: ApiDraft) => Promise<void> }) {
  const { open, setOpen, draft, edit, saving, error, close, submit } = useAddForm(
    EMPTY_API,
    apiProblem,
    onAdd,
    "The API was not added.",
  );

  if (!open) {
    return (
      <button type="button" className="btn btn-small" onClick={() => setOpen(true)}>
        Add an API
      </button>
    );
  }
  return (
    <AddCard
      title="Add an HTTP API"
      note="Each operation becomes one tool. A GET tool runs at once. Other methods ask first."
      error={error}
      saving={saving}
      onSubmit={submit}
      onCancel={close}
    >
      <NameFields name={draft.name} title={draft.title} prefix="api" onChange={edit} />
      <label className="field">
        <span className="label">Operations</span>
        <select
          className="input"
          value={draft.source}
          onChange={(e) => edit({ source: e.target.value as ApiDraft["source"] })}
        >
          {(Object.keys(API_SOURCES) as ApiDraft["source"][]).map((source) => (
            <option key={source} value={source}>
              {API_SOURCES[source]}
            </option>
          ))}
        </select>
      </label>
      {draft.source === "openapi" ? (
        <label className="field">
          <span className="label">OpenAPI document</span>
          <textarea
            className="input"
            rows={6}
            placeholder="openapi: 3.0.3"
            value={draft.openapi}
            onChange={(e) => edit({ openapi: e.target.value })}
          />
          <span className="hint">JSON or YAML, OpenAPI 3.</span>
        </label>
      ) : draft.source === "openapi_url" ? (
        <label className="field">
          <span className="label">Address of the document</span>
          <input
            className="input"
            type="url"
            inputMode="url"
            placeholder="https://api.example.com/openapi.json"
            value={draft.openapiUrl}
            maxLength={2000}
            autoComplete="off"
            onChange={(e) => edit({ openapiUrl: e.target.value })}
          />
        </label>
      ) : (
        <label className="field">
          <span className="label">Endpoints</span>
          <textarea
            className="input"
            rows={4}
            placeholder={"GET /items?limit,cursor list_items\nGET /items/{id}\nPOST /items create_item"}
            value={draft.endpoints}
            onChange={(e) => edit({ endpoints: e.target.value })}
          />
          <span className="hint">One endpoint on each line: the method, the path, and an optional tool name.</span>
        </label>
      )}
      <label className="field">
        <span className="label">{draft.source === "endpoints" ? "Base address" : "Base address (optional)"}</span>
        <input
          className="input"
          type="url"
          inputMode="url"
          placeholder="https://api.example.com/v1"
          value={draft.baseUrl}
          maxLength={500}
          autoComplete="off"
          onChange={(e) => edit({ baseUrl: e.target.value })}
        />
        {draft.source === "endpoints" ? null : (
          <span className="hint">Empty: sama uses the first server of the document.</span>
        )}
      </label>
      <label className="field">
        <span className="label">Sign-in</span>
        <select className="input" value={draft.auth} onChange={(e) => edit({ auth: e.target.value as ApiAuthType })}>
          {(Object.keys(API_AUTH_LABELS) as ApiAuthType[]).map((auth) => (
            <option key={auth} value={auth}>
              {API_AUTH_LABELS[auth]}
            </option>
          ))}
        </select>
        <span className="hint">You give the key, token, or password in the next step, not here.</span>
      </label>
      {draft.auth === "api_key" ? (
        <>
          <label className="field">
            <span className="label">Name of the key</span>
            <input
              className="input"
              value={draft.keyName}
              maxLength={64}
              autoComplete="off"
              onChange={(e) => edit({ keyName: e.target.value })}
            />
          </label>
          <label className="field">
            <span className="label">The key goes into</span>
            <select
              className="input"
              value={draft.keyLocation}
              onChange={(e) => edit({ keyLocation: e.target.value as ApiDraft["keyLocation"] })}
            >
              <option value="header">A header</option>
              <option value="query">A query parameter</option>
            </select>
          </label>
        </>
      ) : null}
      {draft.auth === "oauth2" ? (
        <>
          <label className="field">
            <span className="label">Token URL</span>
            <input
              className="input"
              type="url"
              inputMode="url"
              placeholder="https://auth.example.com/oauth/token"
              value={draft.tokenUrl}
              maxLength={500}
              autoComplete="off"
              onChange={(e) => edit({ tokenUrl: e.target.value })}
            />
          </label>
          <label className="field">
            <span className="label">Scope (optional)</span>
            <input
              className="input"
              value={draft.oauthScope}
              maxLength={400}
              autoComplete="off"
              onChange={(e) => edit({ oauthScope: e.target.value })}
            />
          </label>
          <label className="field">
            <span className="label">The client sends its id and secret in</span>
            <select
              className="input"
              value={draft.clientAuth}
              onChange={(e) => edit({ clientAuth: e.target.value as ApiDraft["clientAuth"] })}
            >
              <option value="basic">Basic auth</option>
              <option value="body">The body of the token request</option>
            </select>
          </label>
        </>
      ) : null}
      <ScopeSelect value={draft.scope} onChange={(scope) => edit({ scope })} />
    </AddCard>
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
      {connection?.kind === "mcp_hosted" && connection.mcp?.custom ? (
        <HostedInfo hosted={connection.mcp.custom as HostedCustom} />
      ) : null}
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

/** What a connector host runs and what it can reach. */
function HostedInfo({ hosted }: { hosted: HostedCustom }) {
  const egress = hosted.egress.length === 0 ? "only the package registry" : hosted.egress.join(", ");
  return (
    <p className={styles.meta}>
      Runs {hostedWhat(hosted)}. Can reach {egress}.
      {hosted.env.length > 0 ? ` Variables: ${hosted.env.join(", ")}.` : ""}
    </p>
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
            <span className={styles.toolName}>{tool.method && tool.path ? `${tool.method} ${tool.path}` : tool.remote_name}</span>
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
