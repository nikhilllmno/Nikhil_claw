"use client";

// The Connections page (docs/architecture.md, Section 10.3). The user connects the tools of a
// workspace here. Secrets go only into the forms of this page, never into a chat.

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  type Connection,
  type ConnectionScope,
  type Connector,
  errorText,
} from "@/lib/api";
import {
  type ConnectionList,
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
} from "@/lib/connections";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Connections.module.css";

const SCOPE_LABELS: Record<ConnectionScope, string> = {
  personal: "Only you",
  workspace: "Whole workspace",
};

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

  const start = useCallback(
    async (workspace: string, connector: string, scope: ConnectionScope) => {
      const started = await api.startConnection(workspace, connector, scope);
      if (isSafeRedirect(started.redirect_url)) {
        window.location.assign(started.redirect_url);
        return;
      }
      replace(started.connection);
      setFormFor(started.connection.id);
    },
    [api, replace],
  );

  // The entry links: `?connect=` starts a connection, `?connection=` checks it after sign-in.
  useEffect(() => {
    if (handled.current || me === null) return;
    handled.current = true;
    const intent = parseConnectionQuery(window.location.search);
    if (intent === null) return;
    history.replaceState(null, "", window.location.pathname + withoutEntryParams(window.location.search));
    // The state changes run after the effect, in the promise.
    const run = async () => {
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
      ) : catalog.data.length === 0 ? (
        <p className="alert">This sama has no connectors.</p>
      ) : (
        <ul className={styles.list}>
          {catalog.data.map((connector) => (
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
      )}
    </div>
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
  const badge =
    tone === "ok" ? "badge badge-accent" : tone === "wait" ? "badge badge-warning" : tone === "error" ? `badge ${styles.badgeError}` : "badge";

  return (
    <div className={styles.scope}>
      <div className={styles.row}>
        <div className={styles.scopeName}>
          <span>{SCOPE_LABELS[scope]}</span>
          <span className={badge}>{connection ? stateLabel(connection.state) : "Not connected"}</span>
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
          {connection !== null && connection.state !== "pending" ? (
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
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      {showForm ? (
        <ConnectForm
          connector={connector}
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
