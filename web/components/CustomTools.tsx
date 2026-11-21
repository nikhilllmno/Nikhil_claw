"use client";

// The custom tools page (docs/architecture.md, Section 10.8). A member adds an HTTP request tool
// or a script tool for themselves, tests it, and reads its versions. Only an admin adds and changes
// workspace and org tools; other members read and use them. Each write applies at once. The
// credential of an HTTP tool goes only into the form: the API never sends it back, and the agent
// never sees it.
//
// `CustomToolsPanel` holds the whole feature for one workspace, so the settings console (M7.10)
// can show it in a section of its own.

import { useCallback, useState } from "react";
import {
  errorText,
  type CustomTool,
  type CustomToolScope,
  type CustomToolTestResult,
  type CustomToolVersion,
  type HttpMethod,
} from "@/lib/api";
import {
  codeTemplate,
  createBody,
  credentialOf,
  definitionOf,
  type Draft,
  draftOf,
  emptyDraft,
  MAX_DESCRIPTION_CHARS,
  POLICY_LABELS,
  SCOPE_LABELS,
  scopeChoices,
  testArguments,
  toolState,
} from "@/lib/customTools";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { PageHeader } from "./PageHeader";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

const METHODS: HttpMethod[] = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"];

export function CustomToolsPage() {
  const { me, meError, reloadMe, workspaces, workspaceId } = useData();
  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the tools" />;
  const workspace = workspaces.find((w) => w.id === workspaceId) ?? null;
  return (
    <div className={styles.page}>
      <PageHeader
        title="Tools"
        meta={
          <>
            {workspace ? workspace.label : "No workspace"} · Your own HTTP request tools and scripts. The agents find
            them with tool search.
          </>
        }
      />
      {workspace === null ? (
        <p className="alert">You are not in a workspace yet. Ask an admin of your org to add you.</p>
      ) : (
        <CustomToolsPanel key={workspace.id} workspaceId={workspace.id} />
      )}
    </div>
  );
}

type Open = { kind: "new" } | { kind: "edit"; tool: CustomTool; version: CustomToolVersion } | null;

export function CustomToolsPanel({ workspaceId }: { workspaceId: string }) {
  const api = useApi();
  const load = useCallback(() => api.customTools(workspaceId), [api, workspaceId]);
  const tools = useLoad(load, "Could not load the tools.");
  // The scopes in which the user can add a tool. Until they load, the form offers "Only me".
  const loadScopes = useCallback(() => api.customToolScopes(workspaceId), [api, workspaceId]);
  const scopes = useLoad(loadScopes, "Could not load the scopes.");
  const [open, setOpen] = useState<Open>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const done = (text: string | null) => {
    setOpen(null);
    setNotice(text);
    tools.reload();
  };

  if (open !== null) {
    return (
      <ToolEditor
        workspaceId={workspaceId}
        scopes={scopeChoices(scopes.data?.scopes ?? null)}
        scopesError={scopes.error}
        onRetryScopes={scopes.reload}
        open={open}
        onDone={done}
        onCancel={() => setOpen(null)}
      />
    );
  }
  return (
    <>
      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button type="button" className="btn btn-primary btn-small" onClick={() => setOpen({ kind: "new" })}>
          Add a tool
        </button>
      </div>
      {tools.error ? (
        <ErrorState title="The tools did not load" message={tools.error} onRetry={tools.reload} />
      ) : tools.data === null ? (
        <LoadingState label="Loading the tools" />
      ) : tools.data.length === 0 ? (
        <p className={styles.meta}>
          There are no tools yet. Add an HTTP request to an API, or a Python or Node script. The agent calls it with
          tool search.
        </p>
      ) : (
        <ul className={styles.list} aria-label="Tools">
          {tools.data.map((tool) => (
            <ToolCard
              // A new key after each change gives the card fresh state (busy flags, versions).
              key={`${tool.id}:${tool.current_version}:${tool.latest?.version}:${tool.has_credential}`}
              tool={tool}
              onChanged={(text) => done(text)}
              onEdit={(version) => setOpen({ kind: "edit", tool, version })}
            />
          ))}
        </ul>
      )}
    </>
  );
}

function ToolCard({
  tool,
  onChanged,
  onEdit,
}: {
  tool: CustomTool;
  onChanged: (notice: string) => void;
  onEdit: (version: CustomToolVersion) => void;
}) {
  const api = useApi();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const shown = tool.current ?? tool.latest;

  const remove = async () => {
    if (!window.confirm(`Delete the tool ${tool.name} and all its versions? The agent can then not use it.`)) return;
    setBusy("delete");
    setError(null);
    try {
      await api.deleteCustomTool(tool.id);
      onChanged(`The tool ${tool.name} is deleted.`);
    } catch (e) {
      setError(errorText(e, "The tool was not deleted."));
    } finally {
      setBusy(null);
    }
  };

  return (
    <li className={`card ${styles.card}`}>
      <div className={styles.cardHead}>
        <div className={styles.name}>
          <span className="mono">{tool.tool_name}</span>
          <span className={tool.scope === "user" ? "badge" : "badge badge-accent"}>{SCOPE_LABELS[tool.scope]}</span>
          <span className="badge">{shown?.kind === "script" ? `${shown.script?.language} script` : "HTTP"}</span>
          {tool.current_version === null ? <span className="badge badge-warning">No running version</span> : null}
        </div>
        <span className={styles.meta}>{toolState(tool)}</span>
      </div>
      {shown ? <p className={styles.text}>{shown.description}</p> : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.actions}>
        {shown && tool.can_manage ? (
          <button type="button" className="btn btn-small" onClick={() => onEdit(shown)}>
            Edit
          </button>
        ) : null}
        {tool.can_manage ? (
          <button
            type="button"
            className="btn btn-danger btn-small"
            disabled={busy !== null}
            onClick={() => void remove()}
          >
            {busy === "delete" ? "Deleting" : "Delete"}
          </button>
        ) : null}
      </div>
      {tool.can_manage && tool.current_version !== null ? <TestPanel tool={tool} /> : null}
      {tool.can_manage && tool.current?.http && tool.current.http.auth.type !== "none" ? (
        <CredentialForm tool={tool} onSaved={onChanged} />
      ) : null}
      <History tool={tool} onChanged={onChanged} />
    </li>
  );
}

function TestPanel({ tool, version }: { tool: CustomTool; version?: CustomToolVersion }) {
  const api = useApi();
  const target = version ?? tool.current;
  const [text, setText] = useState(() => exampleArguments(target?.input_schema));
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CustomToolTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (event: React.FormEvent) => {
    event.preventDefault();
    const args = testArguments(text);
    if (typeof args === "string") {
      setError(args);
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await api.testCustomTool(tool.id, args, version?.version));
    } catch (e) {
      setError(errorText(e, "The test did not run."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className={version ? styles.form : styles.section}
      onSubmit={(event) => void run(event)}
      aria-label={version ? `Test version ${version.version} of ${tool.name}` : `Test ${tool.name}`}
    >
      <h3 className={styles.subheading}>{version ? `Test version ${version.version}` : "Test"}</h3>
      <label className="field">
        <span className="label">Arguments (JSON)</span>
        <textarea
          className="input mono"
          rows={3}
          spellCheck={false}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <span className="hint">
          The test runs {version ? `version ${version.version}` : "the current version"} as the agent would, with the
          saved credential. A script runs in a new sandbox.
        </span>
      </label>
      <div className={styles.actions}>
        <button type="submit" className="btn btn-small" disabled={busy}>
          {busy ? "Running" : "Run the test"}
        </button>
      </div>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      {result ? (
        <div role="status" aria-label="Test result">
          <p className={result.ok ? "alert alert-success" : "alert alert-error"}>
            {result.ok ? `It worked in ${result.ms} ms (version ${result.version}).` : `It failed: ${result.error}`}
          </p>
          {result.ok && result.result !== null ? <pre className={styles.pre}>{result.result}</pre> : null}
        </div>
      ) : null}
    </form>
  );
}

function CredentialForm({ tool, onSaved }: { tool: CustomTool; onSaved: (notice: string) => void }) {
  const api = useApi();
  const basic = tool.current?.http?.auth.type === "basic";
  const [secret, setSecret] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    const credential = basic ? { username, password } : { value: secret };
    if (basic ? !username || !password : !secret) {
      setError(basic ? "Give the user name and the password." : "Give the key or the token.");
      return;
    }
    // The secret leaves the page state before the call.
    setSecret("");
    setPassword("");
    setBusy(true);
    setError(null);
    try {
      await api.setCustomToolCredential(tool.id, credential);
      onSaved(`The credential of ${tool.name} is saved.`);
    } catch (e) {
      setError(errorText(e, "The credential was not saved."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className={styles.section} onSubmit={(event) => void save(event)} aria-label={`Credential of ${tool.name}`}>
      <h3 className={styles.subheading}>Credential</h3>
      <p className={styles.meta}>
        {tool.has_credential
          ? "A credential is saved. Sama sends it only to the address of the current version."
          : "No credential is saved. The tool cannot call the API without it."}
      </p>
      {basic ? (
        <label className="field">
          <span className="label">User name</span>
          <input className="input" autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} />
        </label>
      ) : null}
      <label className="field">
        <span className="label">{basic ? "Password" : "Key or token"}</span>
        <input
          className="input"
          type="password"
          autoComplete="new-password"
          value={basic ? password : secret}
          onChange={(e) => (basic ? setPassword(e.target.value) : setSecret(e.target.value))}
        />
        <span className="hint">It goes to the vault of sama. No page shows it again.</span>
      </label>
      <div className={styles.actions}>
        <button type="submit" className="btn btn-small" disabled={busy}>
          {busy ? "Saving" : tool.has_credential ? "Replace the credential" : "Save the credential"}
        </button>
      </div>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </form>
  );
}

function History({ tool, onChanged }: { tool: CustomTool; onChanged: (notice: string) => void }) {
  const api = useApi();
  const [shown, setShown] = useState(false);
  const load = useCallback(() => api.customToolVersions(tool.id), [api, tool.id]);
  const versions = useLoad(shown ? load : null, "Could not load the versions.");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now] = useState(() => Date.now());

  const act = async (key: string, call: () => Promise<unknown>, notice: string, failed: string) => {
    setBusy(key);
    setError(null);
    try {
      await call();
      onChanged(notice);
    } catch (e) {
      setError(errorText(e, failed));
    } finally {
      setBusy(null);
    }
  };

  if (!shown) {
    return (
      <div className={styles.actions}>
        <button type="button" className="btn btn-quiet btn-small" onClick={() => setShown(true)}>
          Show the versions
        </button>
      </div>
    );
  }
  return (
    <div className={styles.section}>
      <h3 className={styles.subheading}>Versions</h3>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      {versions.error ? (
        <ErrorState title="The versions did not load" message={versions.error} onRetry={versions.reload} />
      ) : versions.data === null ? (
        <LoadingState label="Loading the versions" />
      ) : (
        <ul className={styles.entries}>
          {versions.data.map((v) => (
            <li key={v.version} className={styles.card}>
              <div className={styles.row}>
                <div className={styles.name}>
                  <span>Version {v.version}</span>
                  {v.version === tool.current_version ? <span className="badge badge-accent">Running</span> : null}
                  <span className={styles.meta}>
                    {v.change} · {POLICY_LABELS[v.policy]} · {relativeTime(v.created_at, now)}
                    {v.summary ? ` · ${v.summary}` : ""}
                  </span>
                </div>
                <div className={styles.actions}>
                  {tool.can_manage && v.version !== tool.current_version ? (
                    <button
                      type="button"
                      className="btn btn-quiet btn-small"
                      disabled={busy !== null}
                      onClick={() =>
                        void act(
                          `revert-${v.version}`,
                          () => api.revertCustomTool(tool.id, v.version),
                          `${tool.name} runs version ${v.version} again.`,
                          "The revert did not work.",
                        )
                      }
                    >
                      Revert to this
                    </button>
                  ) : null}
                </div>
              </div>
              <VersionDetails tool={tool} version={v} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ToolEditor({
  workspaceId,
  scopes,
  scopesError,
  onRetryScopes,
  open,
  onDone,
  onCancel,
}: {
  workspaceId: string;
  /** The scopes in which the user can add a tool. */
  scopes: CustomToolScope[];
  /** Set when the scopes did not load: the form offers "Only me" and a retry. */
  scopesError: string | null;
  onRetryScopes: () => void;
  open: Exclude<Open, null>;
  onDone: (notice: string) => void;
  onCancel: () => void;
}) {
  const api = useApi();
  const editing = open.kind === "edit" ? open.tool : null;
  const [draft, setDraft] = useState<Draft>(() =>
    open.kind === "edit" ? draftOf(open.tool, open.version) : emptyDraft("http"),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof Draft>(field: K, value: Draft[K]) => setDraft((all) => ({ ...all, [field]: value }));
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (editing) {
      const definition = definitionOf(draft);
      if (typeof definition === "string") {
        setError(definition);
        return;
      }
      setBusy(true);
      try {
        const saved = await api.editCustomTool(editing.id, {
          ...definition,
          summary: draft.summary.trim(),
        });
        const credential = credentialOf(draft);
        if (credential) await api.setCustomToolCredential(editing.id, credential);
        onDone(`${saved.name} runs version ${saved.current_version} now.`);
      } catch (e) {
        setError(errorText(e, "The tool was not saved."));
        setBusy(false);
      }
      return;
    }
    // Only a person who manages the scope reaches this form, so the credential goes with it.
    const body = createBody(draft);
    if (typeof body === "string") {
      setError(body);
      return;
    }
    setBusy(true);
    try {
      const saved = await api.createCustomTool(workspaceId, body);
      onDone(`The tool ${saved.tool_name} is ready. Test it below, then ask the agent to use it.`);
    } catch (e) {
      setError(errorText(e, "The tool was not saved."));
      setBusy(false);
    }
  };

  return (
    <form
      className={`card ${styles.card} ${styles.form}`}
      onSubmit={(event) => void save(event)}
      aria-label={editing ? `Edit ${editing.name}` : "Add a tool"}
    >
      <h2 className={styles.heading}>{editing ? `Edit ${editing.tool_name}` : "Add a tool"}</h2>
      {editing ? null : (
        <>
          <label className="field">
            <span className="label">Name</span>
            <input
              className="input mono"
              value={draft.name}
              maxLength={40}
              placeholder="weather_now"
              onChange={(e) => set("name", e.target.value.toLowerCase())}
            />
            <span className="hint">
              Lower-case letters, digits, and _. The agent calls it custom_
              {draft.name || "name"}.
            </span>
          </label>
          <label className="field">
            <span className="label">Who can use it</span>
            <select
              className="input"
              value={draft.scope}
              onChange={(e) => set("scope", e.target.value as CustomToolScope)}
            >
              {scopes.map((scope) => (
                <option key={scope} value={scope}>
                  {SCOPE_LABELS[scope]}
                </option>
              ))}
            </select>
            {scopesError ? (
              <span className="hint" role="alert">
                The scopes that you can use did not load, so only &quot;Only me&quot; shows.{" "}
                <button type="button" className="btn btn-quiet btn-small" onClick={onRetryScopes}>
                  Try again
                </button>
              </span>
            ) : scopes.length === 1 ? (
              <span className="hint">An admin adds the tools for the workspace and for the org.</span>
            ) : null}
          </label>
          <label className="field">
            <span className="label">Kind</span>
            <select
              className="input"
              value={draft.kind}
              onChange={(e) => set("kind", e.target.value === "script" ? "script" : "http")}
            >
              <option value="http">HTTP request</option>
              <option value="script">Script (Python or Node, in the sandbox)</option>
            </select>
          </label>
        </>
      )}
      <label className="field">
        <span className="label">Description</span>
        <textarea
          className="input"
          rows={3}
          maxLength={MAX_DESCRIPTION_CHARS}
          value={draft.description}
          onChange={(e) => set("description", e.target.value)}
        />
        <span className="hint">The agent reads this to choose the tool. Say what it does and when to use it.</span>
      </label>
      <label className="field">
        <span className="label">Policy</span>
        <select
          className="input"
          value={draft.policy}
          onChange={(e) => set("policy", e.target.value as Draft["policy"])}
        >
          {(Object.keys(POLICY_LABELS) as Draft["policy"][]).map((policy) => (
            <option key={policy} value={policy}>
              {POLICY_LABELS[policy]}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span className="label">Arguments (JSON schema)</span>
        <textarea
          className={`input ${styles.editor}`}
          spellCheck={false}
          value={draft.schema}
          onChange={(e) => set("schema", e.target.value)}
        />
        <span className="hint">Use each property as a placeholder: {"{{name}}"}.</span>
      </label>
      {draft.kind === "http" ? <HttpFields draft={draft} set={set} /> : <ScriptFields draft={draft} set={set} />}
      <label className="field">
        <span className="label">What changed (optional)</span>
        <input
          className="input"
          value={draft.summary}
          maxLength={500}
          onChange={(e) => set("summary", e.target.value)}
        />
      </label>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button type="submit" className="btn btn-primary btn-small" disabled={busy}>
          {busy ? "Saving" : "Save"}
        </button>
        <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

type Setter = <K extends keyof Draft>(field: K, value: Draft[K]) => void;

function HttpFields({ draft, set }: { draft: Draft; set: Setter }) {
  return (
    <fieldset className={`${styles.fieldset} ${styles.form}`}>
      <legend className={styles.subheading}>HTTP request</legend>
      <label className="field">
        <span className="label">Method</span>
        <select className="input" value={draft.method} onChange={(e) => set("method", e.target.value as HttpMethod)}>
          {METHODS.map((method) => (
            <option key={method}>{method}</option>
          ))}
        </select>
      </label>
      <label className="field">
        <span className="label">URL</span>
        <input className="input mono" value={draft.url} onChange={(e) => set("url", e.target.value)} />
        <span className="hint">
          https only. A placeholder can be in the path and the query, not in the host. Private addresses are refused.
        </span>
      </label>
      <label className="field">
        <span className="label">Headers (optional)</span>
        <textarea
          className="input mono"
          rows={2}
          spellCheck={false}
          placeholder="X-Units: {{units}}"
          value={draft.headers}
          onChange={(e) => set("headers", e.target.value)}
        />
        <span className="hint">
          One &quot;Name: value&quot; on each line. Put a key or a token in the auth, not here.
        </span>
      </label>
      {draft.method !== "GET" && draft.method !== "HEAD" ? (
        <label className="field">
          <span className="label">Body (JSON, optional)</span>
          <textarea
            className="input mono"
            rows={4}
            spellCheck={false}
            placeholder={'{"city": "{{city}}"}'}
            value={draft.body}
            onChange={(e) => set("body", e.target.value)}
          />
          <span className="hint">A string that is only a placeholder gets the argument as a JSON value.</span>
        </label>
      ) : null}
      <label className="field">
        <span className="label">Auth</span>
        <select
          className="input"
          value={draft.authType}
          onChange={(e) => set("authType", e.target.value as Draft["authType"])}
        >
          <option value="none">None</option>
          <option value="bearer">Bearer token</option>
          <option value="api_key">API key</option>
          <option value="basic">User name and password</option>
        </select>
      </label>
      {draft.authType === "api_key" ? (
        <div className={styles.toolbar}>
          <label className="field">
            <span className="label">Send the key in</span>
            <select
              className="input"
              value={draft.authWhere}
              onChange={(e) => set("authWhere", e.target.value === "query" ? "query" : "header")}
            >
              <option value="header">A header</option>
              <option value="query">A query parameter</option>
            </select>
          </label>
          <label className={`field ${styles.grow}`}>
            <span className="label">Name</span>
            <input className="input mono" value={draft.authName} onChange={(e) => set("authName", e.target.value)} />
          </label>
        </div>
      ) : null}
      {draft.authType !== "none" ? (
        draft.authType === "basic" ? (
          <div className={styles.toolbar}>
            <label className={`field ${styles.grow}`}>
              <span className="label">User name</span>
              <input
                className="input"
                autoComplete="off"
                value={draft.username}
                onChange={(e) => set("username", e.target.value)}
              />
            </label>
            <label className={`field ${styles.grow}`}>
              <span className="label">Password</span>
              <input
                className="input"
                type="password"
                autoComplete="new-password"
                value={draft.password}
                onChange={(e) => set("password", e.target.value)}
              />
            </label>
          </div>
        ) : (
          <label className="field">
            <span className="label">Key or token</span>
            <input
              className="input"
              type="password"
              autoComplete="new-password"
              value={draft.secret}
              onChange={(e) => set("secret", e.target.value)}
            />
            <span className="hint">
              It goes to the vault of sama. Sama sends it only to the address of this tool. Empty: keep the saved one.
            </span>
          </label>
        )
      ) : null}
      <div className={styles.toolbar}>
        <label className={`field ${styles.grow}`}>
          <span className="label">Answer path (optional)</span>
          <input
            className="input mono"
            placeholder="data.items"
            value={draft.responsePath}
            onChange={(e) => set("responsePath", e.target.value)}
          />
        </label>
        <label className={`field ${styles.grow}`}>
          <span className="label">Keep only these fields (optional)</span>
          <input
            className="input mono"
            placeholder="id, name"
            value={draft.responseFields}
            onChange={(e) => set("responseFields", e.target.value)}
          />
        </label>
      </div>
    </fieldset>
  );
}

function ScriptFields({ draft, set }: { draft: Draft; set: Setter }) {
  return (
    <fieldset className={`${styles.fieldset} ${styles.form}`}>
      <legend className={styles.subheading}>Script</legend>
      <div className={styles.toolbar}>
        <label className="field">
          <span className="label">Language</span>
          <select
            className="input"
            value={draft.language}
            onChange={(e) => {
              const language = e.target.value === "node" ? "node" : "python";
              // A template that the user did not change follows the language.
              if (draft.code === codeTemplate(draft.language)) set("code", codeTemplate(language));
              set("language", language);
            }}
          >
            <option value="python">Python</option>
            <option value="node">Node</option>
          </select>
        </label>
        <label className="field">
          <span className="label">Time limit (seconds)</span>
          <input
            className="input"
            type="number"
            min={1}
            max={120}
            value={draft.timeout}
            onChange={(e) => set("timeout", e.target.value)}
          />
        </label>
      </div>
      <label className="field">
        <span className="label">Code</span>
        <textarea
          className={`input ${styles.editor}`}
          spellCheck={false}
          value={draft.code}
          onChange={(e) => set("code", e.target.value)}
        />
        <span className="hint">
          The script reads the arguments as JSON on standard input and writes one JSON value to standard output. It runs
          in the session sandbox, with no secrets.
        </span>
      </label>
    </fieldset>
  );
}

/** The whole definition of one version, and a test of that version for a person who manages the
 *  tool. The definition never holds a credential. */
function VersionDetails({ tool, version }: { tool: CustomTool; version: CustomToolVersion }) {
  const { kind, description, input_schema, policy, http, script } = version;
  const shown = {
    kind,
    description,
    policy,
    input_schema,
    ...(http ? { http } : {}),
    ...(script ? { script } : {}),
  };
  return (
    <details>
      <summary className={styles.meta}>Show version {version.version}</summary>
      <pre className={styles.pre} aria-label={`Definition of version ${version.version}`}>
        {JSON.stringify(shown, null, 2)}
      </pre>
      {tool.can_manage ? <TestPanel tool={tool} version={version} /> : null}
    </details>
  );
}

/** A JSON object with one example value for each property of the schema. */
function exampleArguments(schema: Record<string, unknown> | undefined): string {
  const properties = (schema?.properties ?? {}) as Record<string, { type?: unknown }>;
  const example: Record<string, unknown> = {};
  for (const [name, spec] of Object.entries(properties)) {
    const kind = spec?.type;
    example[name] =
      kind === "integer" || kind === "number"
        ? 1
        : kind === "boolean"
          ? true
          : kind === "array"
            ? []
            : kind === "object"
              ? {}
              : "";
  }
  return JSON.stringify(example, null, 2);
}
