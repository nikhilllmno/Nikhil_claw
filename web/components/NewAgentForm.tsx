"use client";

// The form for a new agent profile (docs/architecture.md, Section 17.6, M6.9). Only an org owner
// or admin sees it. A template fills the name and the soul; the admin can change both. The new
// agent gets the tools of a new org and the run budget of the plan.

import { useState } from "react";
import { type AgentProfile, errorText, type Org } from "@/lib/api";
import { AGENT_TEMPLATES } from "@/lib/onboarding";
import { useApi } from "./AuthProvider";
import styles from "./Page.module.css";

const NAME_MAX = 80;
const SOUL_MAX = 20_000;

export function NewAgentForm({
  org,
  workspaceId,
  onCreated,
  onCancel,
}: {
  org: Org;
  /** The workspace to offer for a profile of one workspace. */
  workspaceId: string | null;
  onCreated: (profile: AgentProfile) => void;
  onCancel?: () => void;
}) {
  const api = useApi();
  const first = AGENT_TEMPLATES[0];
  const [template, setTemplate] = useState<string>(first.id);
  const [name, setName] = useState<string>(first.name);
  const [soul, setSoul] = useState<string>(first.soul);
  const [scope, setScope] = useState<"org" | "workspace">("org");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const workspace = org.workspaces.find((w) => w.id === workspaceId) ?? null;

  const choose = (id: string) => {
    const chosen = AGENT_TEMPLATES.find((t) => t.id === id);
    if (!chosen) return;
    setTemplate(chosen.id);
    setName(chosen.name);
    setSoul(chosen.soul);
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const clean = name.trim();
    if (clean === "") {
      setError("Give the agent a name.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const profile = await api.createAgentProfile(org.id, {
        name: clean,
        soul,
        workspace_id: scope === "workspace" && workspace ? workspace.id : null,
      });
      onCreated(profile);
    } catch (e) {
      setError(errorText(e, "The agent was not created."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className={styles.form} onSubmit={submit} aria-label="Create an agent">
      <label className="field">
        <span className="label">Start from</span>
        <select className="input" value={template} onChange={(e) => choose(e.target.value)} disabled={busy}>
          {AGENT_TEMPLATES.map((t) => (
            <option key={t.id} value={t.id}>
              {t.title}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span className="label">Name</span>
        <input
          className="input"
          value={name}
          maxLength={NAME_MAX}
          required
          onChange={(e) => setName(e.target.value)}
          disabled={busy}
        />
      </label>
      <label className="field">
        <span className="label">Soul</span>
        <textarea
          className="input"
          rows={4}
          value={soul}
          maxLength={SOUL_MAX}
          onChange={(e) => setSoul(e.target.value)}
          disabled={busy}
        />
        <span className="hint">
          The soul tells the agent who it is and how it works. Never put a password or a key in it.
        </span>
      </label>
      {workspace ? (
        <label className="field">
          <span className="label">Who can use it</span>
          <select
            className="input"
            value={scope}
            onChange={(e) => setScope(e.target.value === "workspace" ? "workspace" : "org")}
            disabled={busy}
          >
            <option value="org">Every workspace of the org</option>
            <option value="workspace">Only the workspace {workspace.name}</option>
          </select>
        </label>
      ) : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button type="submit" className="btn btn-primary btn-small" disabled={busy}>
          {busy ? "Creating…" : "Create the agent"}
        </button>
        {onCancel ? (
          <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
        ) : null}
      </div>
    </form>
  );
}
