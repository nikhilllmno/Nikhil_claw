"use client";

// The org API keys (docs/architecture.md, Section 10.7, M7.6). An org admin makes a key for the
// OpenAI-compatible endpoint and the MCP server, sees the keys of the org, and revokes a key.
// A new key shows one time on this card. The API never sends it again.

import { useCallback, useState } from "react";
import { errorText, type ApiKeyCreated, type Org } from "@/lib/api";
import {
  approvalsText,
  clientSnippets,
  draftBody,
  emptyDraft,
  MAX_EXPIRY_DAYS,
  NAME_MAX_CHARS,
  profilesFor,
  SCOPE_TITLES,
  statusBadge,
  type ApiKeyDraft,
} from "@/lib/apikeys";
import { apiUrl } from "@/lib/config";
import { relativeTime, timeLeft } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

export function ApiKeys({ org }: { org: Org }) {
  const api = useApi();
  const load = useCallback(() => api.apiKeys(org.id), [api, org.id]);
  const keys = useLoad(org.admin && org.region_here ? load : null, "Could not load the API keys.");
  const [draft, setDraft] = useState<ApiKeyDraft>(() => emptyDraft(org));
  const [made, setMade] = useState<ApiKeyCreated | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [now] = useState(() => Date.now());
  const profiles = profilesFor(org, draft.workspaceId);
  const snippets = clientSnippets(apiUrl() || "https://<sama address>");

  const change = (part: Partial<ApiKeyDraft>) => setDraft((all) => ({ ...all, ...part }));

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setNotice(null);
    const checked = draftBody(draft);
    if ("problem" in checked) {
      setError(checked.problem);
      return;
    }
    setBusy("create");
    try {
      const key = await api.createApiKey(org.id, checked.body);
      setMade(key);
      setCopied(false);
      setDraft(emptyDraft(org));
      keys.setData([key, ...(keys.data ?? [])]);
    } catch (e) {
      setError(errorText(e, "The key was not made."));
    } finally {
      setBusy(null);
    }
  };

  const revoke = async (id: string, name: string) => {
    if (!window.confirm(`Revoke the key ${name}? Each client that uses it stops at once.`)) return;
    setBusy(`revoke-${id}`);
    setError(null);
    setNotice(null);
    try {
      await api.revokeApiKey(org.id, id);
      keys.setData(await api.apiKeys(org.id));
      if (made?.id === id) setMade(null);
      setNotice(`The key ${name} is revoked.`);
    } catch (e) {
      setError(errorText(e, "The key was not revoked."));
    } finally {
      setBusy(null);
    }
  };

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
      setError("Could not copy the key. Select it and copy it.");
    }
  };

  if (!org.region_here) {
    return (
      <section className={`card ${styles.card}`} aria-label="API keys">
        <p className={styles.meta}>Another deployment serves this org. Manage its API keys there.</p>
      </section>
    );
  }
  if (!org.admin) {
    return (
      <section className={`card ${styles.card}`} aria-label="API keys">
        <p className={styles.meta}>An org admin manages the API keys of the org.</p>
      </section>
    );
  }

  return (
    <section className={`card ${styles.card}`} aria-label={`API keys of ${org.name}`}>
      <div className={styles.cardHead}>
        <h2 className={styles.heading}>API keys</h2>
        <span className={styles.meta}>{org.name}</span>
      </div>
      <p className={styles.text}>
        Other agents and apps use sama with an API key: the OpenAI-compatible chat endpoint at /v1 and the MCP server at
        /mcp. A key acts as you in one workspace. Its runs use the policy, the approvals, and the budgets of the agent.
        The key stops when you revoke it or when you are no longer an org admin.
      </p>

      {made ? (
        <div className={styles.section} role="status" aria-label="The new key">
          <p className="alert alert-success">Copy the key now. Sama shows it only this one time.</p>
          <div className={styles.row}>
            <input
              className="input mono"
              readOnly
              value={made.key}
              aria-label="The new API key"
              onFocus={(e) => e.target.select()}
            />
            <div className={styles.actions}>
              <button type="button" className="btn btn-small" onClick={() => void copy(made.key)}>
                {copied ? "Copied" : "Copy"}
              </button>
              <button type="button" className="btn btn-small" onClick={() => setMade(null)}>
                Done
              </button>
            </div>
          </div>
          <p className={styles.meta}>OpenAI Python SDK:</p>
          <pre className={styles.pre}>{snippets.openai}</pre>
          <p className={styles.meta}>Claude Code (MCP):</p>
          <pre className={styles.pre}>{snippets.mcp}</pre>
        </div>
      ) : null}

      <form className={styles.form} onSubmit={(event) => void create(event)} aria-label="Make an API key">
        <label className="field">
          <span className="label">Name</span>
          <input
            className="input"
            value={draft.name}
            maxLength={NAME_MAX_CHARS}
            onChange={(e) => change({ name: e.target.value })}
            placeholder="For example: Claude Code of the team"
          />
        </label>
        <label className="field">
          <span className="label">Workspace</span>
          <select
            className="input"
            value={draft.workspaceId}
            onChange={(e) => change({ workspaceId: e.target.value, profileId: "" })}
          >
            {org.workspaces.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="label">Agent</span>
          <select className="input" value={draft.profileId} onChange={(e) => change({ profileId: e.target.value })}>
            <option value="">Each agent of the workspace</option>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <fieldset className="field">
          <legend className="label">Use</legend>
          <label>
            <input type="checkbox" checked={draft.chat} onChange={(e) => change({ chat: e.target.checked })} />{" "}
            {SCOPE_TITLES.chat}
          </label>
          <label>
            <input type="checkbox" checked={draft.mcp} onChange={(e) => change({ mcp: e.target.checked })} />{" "}
            {SCOPE_TITLES.mcp}
          </label>
        </fieldset>
        <label className="field">
          <span className="label">Approvals</span>
          <select
            className="input"
            value={draft.approvals}
            onChange={(e) =>
              change({
                approvals: e.target.value === "pending" ? "pending" : "deny",
              })
            }
          >
            <option value="deny">Deny: no person watches the runs</option>
            <option value="pending">Wait: a person approves in the web app</option>
          </select>
          <span className="hint">{approvalsText(draft.approvals)}</span>
        </label>
        <label className="field">
          <span className="label">Expires after (days)</span>
          <input
            className="input"
            inputMode="numeric"
            value={draft.expiresInDays}
            onChange={(e) => change({ expiresInDays: e.target.value })}
            placeholder={`Empty: no expiry. At most ${MAX_EXPIRY_DAYS}.`}
          />
        </label>
        <div className={styles.actions}>
          <button type="submit" className="btn btn-primary btn-small" disabled={busy !== null}>
            {busy === "create" ? "Making" : "Make key"}
          </button>
        </div>
      </form>

      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}

      {keys.error ? (
        <ErrorState title="The API keys did not load" message={keys.error} onRetry={keys.reload} />
      ) : keys.data === null ? (
        <LoadingState label="Loading the API keys" />
      ) : keys.data.length === 0 ? (
        <p className={styles.meta}>The org has no API keys. Make a key to connect another agent or app.</p>
      ) : (
        keys.data.map((key) => {
          const badge = statusBadge(key);
          return (
            <div key={key.id} className={styles.section}>
              <div className={styles.row}>
                <div className={styles.name}>
                  <span>{key.name}</span>
                  <span className={badge.className}>{badge.label}</span>
                  <span className="badge mono">{key.prefix}…</span>
                </div>
                {key.status === "active" ? (
                  <button
                    type="button"
                    className="btn btn-danger btn-small"
                    disabled={busy !== null}
                    onClick={() => void revoke(key.id, key.name)}
                  >
                    {busy === `revoke-${key.id}` ? "Revoking" : "Revoke"}
                  </button>
                ) : null}
              </div>
              <p className={styles.meta}>
                {key.workspace_name} · {key.agent_profile_name ?? "each agent"} ·{" "}
                {key.scopes.map((s) => SCOPE_TITLES[s]).join(", ")} · {approvalsText(key.approvals)}
              </p>
              <p className={styles.meta}>
                Made by {key.user_name} {relativeTime(key.created_at, now)}.{" "}
                {key.last_used_at ? `Last used ${relativeTime(key.last_used_at, now)}.` : "Not used yet."}{" "}
                {key.expires_at && key.status === "active" ? `Expires ${timeLeft(key.expires_at, now)}.` : ""}
              </p>
            </div>
          );
        })
      )}
    </section>
  );
}
