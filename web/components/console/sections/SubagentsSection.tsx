"use client";

// The Sub-agents section (M7.10): the sub-agents of an agent. A sub-agent is a helper with its own
// prompt and tools that the agent can hand work to. The whole list is saved through
// `PATCH /orgs/{org_id}/agent-profiles/{id}` (`subagents`).

import { useCallback, useState } from "react";
import { type AgentProfile, errorText } from "@/lib/api";
import { canManageAgents } from "@/lib/console";
import {
  type DraftSubagent,
  newSubagent,
  runtimeLabel,
  runtimeOption,
  toDraft,
  toSubagent,
  validateDraft,
} from "@/lib/profile";
import { useApi } from "../../AuthProvider";
import { useData } from "../../DataProvider";
import { ErrorState, LoadingState } from "../../ui";
import { useLoad } from "../../useLoad";
import styles from "../../Page.module.css";
import admin from "../../Admin.module.css";
import { ToneBadge, useAgentChoice } from "../kit";
import { ComingCard, ProfilePicker, SectionHeader } from "../parts";

export function SubagentsSection() {
  const { org, profileId, setProfileId } = useAgentChoice();
  const mayManage = canManageAgents(org);

  return (
    <>
      <SectionHeader
        id="subagents"
        meta={mayManage ? null : "Only an org admin with the right to manage agents can change sub-agents."}
      />
      {profileId === null ? (
        <div className="state">
          <h2>No agents</h2>
          <p>This org has no agent yet. Make one in the section Harness.</p>
        </div>
      ) : (
        <>
          <ProfilePicker profiles={org.agent_profiles} value={profileId} onChange={setProfileId} />
          <AgentSubagents key={profileId} orgId={org.id} profileId={profileId} manage={mayManage} />
        </>
      )}
      <ComingCard title="Sub-agent library">
        Personal, workspace, and org sub-agents will come with the sub-agent library. Today a sub-agent belongs to one
        agent.
      </ComingCard>
    </>
  );
}

function AgentSubagents({ orgId, profileId, manage }: { orgId: string; profileId: string; manage: boolean }) {
  const api = useApi();
  const { reloadMe } = useData();
  const load = useCallback(() => api.getAgentProfile(orgId, profileId), [api, orgId, profileId]);
  const { data: profile, setData: setProfile, error, reload } = useLoad(load, "Could not load the agent.");

  if (error) return <ErrorState title="The agent did not load" message={error} onRetry={reload} />;
  if (profile === null) return <LoadingState label="Loading the agent" />;
  return (
    <SubagentList
      orgId={orgId}
      profile={profile}
      readOnly={!manage || !profile.editable}
      onSaved={(saved) => {
        setProfile(saved);
        reloadMe();
      }}
    />
  );
}

type View = { kind: "list" } | { kind: "form"; uid: number | null };

function SubagentList({
  orgId,
  profile,
  readOnly,
  onSaved,
}: {
  orgId: string;
  profile: AgentProfile;
  readOnly: boolean;
  onSaved: (profile: AgentProfile) => void;
}) {
  const api = useApi();
  const { limits } = profile.options;
  const runtime = runtimeOption(profile.options, profile.runtime, profile.runtime);
  const [subs, setSubs] = useState<DraftSubagent[]>(() => toDraft(profile).subagents);
  const [view, setView] = useState<View>({ kind: "list" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  /** Saves `next` as the whole list. It gives true when the API took it. */
  const saveList = async (next: DraftSubagent[], done: string): Promise<boolean> => {
    const problem = validateDraft({ ...toDraft(profile), subagents: next }, limits, runtime);
    if (problem) {
      setError(problem);
      return false;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.updateAgentProfile(orgId, profile.id, { subagents: next.map(toSubagent) });
      onSaved(result);
      setSubs(toDraft(result).subagents);
      setNotice(done);
      return true;
    } catch (e) {
      setError(errorText(e, "The sub-agents were not saved."));
      return false;
    } finally {
      setSaving(false);
    }
  };

  const remove = async (sub: DraftSubagent) => {
    if (!window.confirm(`Delete the sub-agent ${sub.name}?`)) return;
    await saveList(
      subs.filter((s) => s.uid !== sub.uid),
      `The sub-agent ${sub.name} is deleted.`,
    );
  };

  if (view.kind === "form") {
    const editing = view.uid === null ? null : (subs.find((s) => s.uid === view.uid) ?? null);
    return (
      <SubagentForm
        key={view.uid ?? "new"}
        profile={profile}
        initial={editing ?? newSubagent()}
        isNew={editing === null}
        readOnly={readOnly || !runtime.subagents}
        saving={saving}
        error={error}
        onCancel={() => {
          setError(null);
          setView({ kind: "list" });
        }}
        onSave={async (sub) => {
          const next = editing === null ? [...subs, sub] : subs.map((s) => (s.uid === sub.uid ? sub : s));
          const ok = await saveList(
            next,
            editing === null ? `The sub-agent ${sub.name.trim()} is created.` : `The sub-agent ${sub.name.trim()} is saved.`,
          );
          if (ok) setView({ kind: "list" });
        }}
      />
    );
  }

  const full = subs.length >= limits.max_subagents;
  return (
    <section className={`card ${styles.card}`} aria-labelledby="sub-list">
      <div className={styles.cardHead}>
        <h2 id="sub-list" className={styles.heading}>
          Sub-agents of {profile.name}
        </h2>
        {readOnly || !runtime.subagents ? null : (
          <button
            type="button"
            className="btn btn-primary btn-small"
            disabled={full || saving}
            onClick={() => {
              setNotice(null);
              setError(null);
              setView({ kind: "form", uid: null });
            }}
          >
            New sub-agent
          </button>
        )}
      </div>
      {!runtime.subagents ? (
        <p className="alert" role="note">
          The {runtimeLabel(profile.runtime)} runtime has no sub-agents. Choose another runtime in the section Harness
          to use them.
        </p>
      ) : null}
      {full && !readOnly && runtime.subagents ? (
        <p className="hint">An agent has at most {limits.max_subagents} sub-agents. Delete one to add another.</p>
      ) : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      {subs.length === 0 ? (
        <p className="hint">
          This agent has no sub-agents.
          {runtime.subagents ? " A sub-agent is a helper with its own prompt and tools that the agent can hand work to." : ""}
        </p>
      ) : (
        <ul className={styles.list}>
          {subs.map((sub) => (
            <li key={sub.uid} className={styles.entry}>
              <div className={styles.row}>
                <div className={styles.name}>{sub.name}</div>
                <div className={styles.actions}>
                  <button
                    type="button"
                    className="btn btn-small"
                    onClick={() => {
                      setNotice(null);
                      setError(null);
                      setView({ kind: "form", uid: sub.uid });
                    }}
                  >
                    {readOnly || !runtime.subagents ? "View" : "Edit"}
                  </button>
                  {readOnly ? null : (
                    <button
                      type="button"
                      className="btn btn-small btn-danger"
                      disabled={saving}
                      onClick={() => remove(sub)}
                    >
                      Delete
                    </button>
                  )}
                </div>
              </div>
              <p className={styles.text}>{sub.description}</p>
              <div className={admin.chips}>
                <span className={admin.chip}>{sub.model === "" ? "Model of the agent" : sub.model}</span>
                {sub.maxTurns === "" ? null : <span className={admin.chip}>{sub.maxTurns} turns at most</span>}
                {sub.toolsets.map((name) => (
                  <span key={name} className={admin.chip}>
                    {name}
                  </span>
                ))}
                {sub.toolsets.length === 0 ? <ToneBadge tone="neutral">No tool groups</ToneBadge> : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function SubagentForm({
  profile,
  initial,
  isNew,
  readOnly,
  saving,
  error,
  onCancel,
  onSave,
}: {
  profile: AgentProfile;
  initial: DraftSubagent;
  isNew: boolean;
  readOnly: boolean;
  saving: boolean;
  error: string | null;
  onCancel: () => void;
  onSave: (sub: DraftSubagent) => void;
}) {
  const { limits } = profile.options;
  const runtime = runtimeOption(profile.options, profile.runtime, profile.runtime);
  const [sub, setSub] = useState<DraftSubagent>(initial);
  const edit = (change: Partial<DraftSubagent>) => setSub((s) => ({ ...s, ...change }));
  const choices = [...new Set([...profile.toolsets, ...sub.toolsets])];
  const models = sub.model === "" || runtime.models.includes(sub.model) ? runtime.models : [sub.model, ...runtime.models];
  const toggle = (name: string) =>
    edit({ toolsets: sub.toolsets.includes(name) ? sub.toolsets.filter((n) => n !== name) : [...sub.toolsets, name] });

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!readOnly) onSave(sub);
  };

  return (
    <form className={`card ${styles.card}`} onSubmit={submit} aria-labelledby="sub-form">
      <div className={styles.cardHead}>
        <h2 id="sub-form" className={styles.heading}>
          {isNew ? "New sub-agent" : sub.name || "Sub-agent"}
        </h2>
        <button type="button" className="btn btn-quiet btn-small" onClick={onCancel}>
          Back to the list
        </button>
      </div>
      <label className="field">
        <span className="label">Name</span>
        <input
          className="input"
          value={sub.name}
          maxLength={limits.subagent_name_max_chars}
          readOnly={readOnly}
          onChange={(e) => edit({ name: e.target.value })}
        />
        <span className="hint">Lowercase letters, digits, and hyphens, for example code-reviewer.</span>
      </label>
      <label className="field">
        <span className="label">Description</span>
        <input
          className="input"
          value={sub.description}
          maxLength={limits.subagent_description_max_chars}
          readOnly={readOnly}
          onChange={(e) => edit({ description: e.target.value })}
        />
        <span className="hint">When the agent should use this sub-agent.</span>
      </label>
      <label className="field">
        <span className="label">Prompt</span>
        <textarea
          className={`input ${styles.editor}`}
          rows={6}
          value={sub.prompt}
          maxLength={limits.subagent_prompt_max_chars}
          readOnly={readOnly}
          onChange={(e) => edit({ prompt: e.target.value })}
        />
      </label>
      <fieldset className={admin.choices} disabled={readOnly}>
        <legend className="label">Tool groups</legend>
        {choices.length === 0 ? <p className="hint">Turn on a tool group for the agent first, in the section Tools.</p> : null}
        {choices.map((name) => (
          <label key={name} className={admin.choice}>
            <input type="checkbox" checked={sub.toolsets.includes(name)} onChange={() => toggle(name)} />
            <span className={admin.choiceText}>
              <strong>{name}</strong>
              {profile.toolsets.includes(name) ? null : <span className="hint">Off for the agent</span>}
            </span>
          </label>
        ))}
      </fieldset>
      <div className={admin.grid}>
        <label className="field">
          <span className="label">Model</span>
          <select
            className="input"
            value={sub.model}
            disabled={readOnly}
            onChange={(e) => edit({ model: e.target.value })}
          >
            <option value="">Same as the agent</option>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="label">Max turns</span>
          <input
            className="input"
            type="number"
            inputMode="numeric"
            min={1}
            max={limits.subagent_max_turns}
            value={sub.maxTurns}
            readOnly={readOnly}
            onChange={(e) => edit({ maxTurns: e.target.value })}
          />
          <span className="hint">Empty means the default. At most {limits.subagent_max_turns}.</span>
        </label>
      </div>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      {readOnly ? (
        <p className="alert" role="note">
          You can read this sub-agent but not change it.
        </p>
      ) : (
        <div className={styles.actions}>
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? "Saving…" : isNew ? "Create the sub-agent" : "Save changes"}
          </button>
          <button type="button" className="btn" disabled={saving} onClick={onCancel}>
            Cancel
          </button>
        </div>
      )}
    </form>
  );
}
