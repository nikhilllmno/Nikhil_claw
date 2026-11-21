"use client";

import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { type AgentProfile, errorText } from "@/lib/api";
import {
  BUSY_MODES,
  type Draft,
  type DraftSubagent,
  diffDraft,
  newSubagent,
  runtimeLabel,
  runtimeOption,
  toDraft,
  validateDraft,
  withCurrent,
  withRuntime,
} from "@/lib/profile";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { ProfileHistory } from "./ProfileHistory";
import { ProfileLibrarySubagents } from "./ProfileLibrarySubagents";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./AgentSettings.module.css";

export function AgentSettings({ profileId }: { profileId: string }) {
  const api = useApi();
  const { me, meError, reloadMe, orgOfProfile } = useData();
  const org = orgOfProfile(profileId);
  const orgId = org?.id ?? null;
  const loadProfile = useMemo(
    () => (orgId === null ? null : () => api.getAgentProfile(orgId, profileId)),
    [api, orgId, profileId],
  );
  const {
    data: profile,
    setData: setProfile,
    error: loadError,
    reload,
  } = useLoad(loadProfile, "Could not load the agent.");
  // A revert changes the saved values under the form, so the form starts again from them.
  const [revision, setRevision] = useState(0);

  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the agent" />;
  if (org === null) {
    return (
      <ErrorState
        title="Agent not found"
        message="This agent does not exist, or you cannot see it."
      />
    );
  }
  if (loadError) {
    return <ErrorState title="The agent did not load" message={loadError} onRetry={reload} />;
  }
  if (profile === null) return <LoadingState label="Loading the agent" />;
  return (
    <SettingsForm
      key={`${profile.id}:${revision}`}
      orgId={org.id}
      profile={profile}
      onSaved={(saved) => {
        setProfile(saved);
        reloadMe(); // The name and the model show in the sidebar.
      }}
    >
      <ProfileHistory
        orgId={org.id}
        profile={profile}
        onReverted={(reverted) => {
          setProfile(reverted);
          setRevision((n) => n + 1);
          reloadMe();
        }}
      />
    </SettingsForm>
  );
}

function SettingsForm({
  orgId,
  profile,
  onSaved,
  children,
}: {
  orgId: string;
  profile: AgentProfile;
  onSaved: (profile: AgentProfile) => void;
  /** More sections after the settings, such as the history. */
  children?: ReactNode;
}) {
  const api = useApi();
  const { options } = profile;
  const { workspaceId } = useData();
  const readOnly = !profile.editable;
  const [draft, setDraft] = useState<Draft>(() => toDraft(profile));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The draft that the last save made. The form shows "Saved" only while it is the draft.
  const [savedDraft, setSavedDraft] = useState<Draft | null>(null);

  const patch = useMemo(() => diffDraft(profile, draft), [profile, draft]);
  const dirty = Object.keys(patch).length > 0;
  // The runtime of the draft: its models and its sub-agent support.
  const runtime = useMemo(
    () => runtimeOption(options, profile.runtime, draft.runtime),
    [options, profile.runtime, draft.runtime],
  );
  const problem = useMemo(
    () => validateDraft(draft, options.limits, runtime),
    [draft, options.limits, runtime],
  );

  const edit = useCallback((change: Partial<Draft>) => {
    setDraft((d) => ({ ...d, ...change }));
  }, []);
  const saved = savedDraft === draft;

  // Warn before the page closes with unsaved changes.
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!dirty || problem || readOnly) return;
    const submitted = draft;
    setSaving(true);
    setError(null);
    try {
      const result = await api.updateAgentProfile(orgId, profile.id, patch);
      onSaved(result);
      // Edits made while the request ran stay: then the form is still dirty.
      const fresh = toDraft(result);
      setDraft((current) => (current === submitted ? fresh : current));
      setSavedDraft(fresh);
    } catch (e) {
      setError(errorText(e, "The changes were not saved."));
    } finally {
      setSaving(false);
    }
  };

  const { soul_max_chars: soulMax, max_subagents: maxSubagents } = options.limits;
  const models = withCurrent(runtime.models, draft.model);
  const runtimes = withCurrent(
    options.runtimes.map((r) => r.name),
    draft.runtime,
  );
  const toggle = (list: string[], name: string) =>
    list.includes(name) ? list.filter((n) => n !== name) : [...list, name];

  const updateSub = (uid: number, change: Partial<DraftSubagent>) =>
    edit({ subagents: draft.subagents.map((s) => (s.uid === uid ? { ...s, ...change } : s)) });

  return (
    <form className={styles.page} onSubmit={save}>
      <header className={styles.header}>
        <Link href="/" className={styles.back}>
          Back to sessions
        </Link>
        <h1 className={styles.title}>{profile.name}</h1>
        <p className={styles.meta}>
          {runtimeLabel(profile.runtime)} · {profile.workspace_id === null ? "available to the whole org" : "one workspace"}
        </p>
      </header>

      {readOnly ? (
        <p className="alert" role="note">
          You can read these settings but not change them. Only an org owner or admin can edit an agent.
        </p>
      ) : (
        <p className="alert" role="note">
          Changes apply to new sessions. A tool group that you turn off stops at the next tool call in every session.
        </p>
      )}

      <section className={`card ${styles.section}`}>
        <h2 className={styles.heading}>General</h2>
        <label className="field">
          <span className="label">Name</span>
          <input
            className="input"
            value={draft.name}
            maxLength={options.limits.name_max_chars}
            readOnly={readOnly}
            onChange={(e) => edit({ name: e.target.value })}
          />
        </label>
        <label className="field">
          <span className="label">Runtime</span>
          <select
            className="input"
            value={draft.runtime}
            disabled={readOnly}
            onChange={(e) => setDraft((d) => withRuntime(d, profile, e.target.value))}
          >
            {runtimes.map((r) => (
              <option key={r} value={r}>
                {runtimeLabel(r)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="label">Model</span>
          <select
            className="input"
            value={draft.model}
            disabled={readOnly}
            onChange={(e) => edit({ model: e.target.value })}
          >
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
      </section>

      <section className={`card ${styles.section}`}>
        <h2 className={styles.heading}>While the agent works</h2>
        <label className="field">
          <span className="label">A new message</span>
          <select
            className="input"
            value={draft.busyMode}
            disabled={readOnly}
            onChange={(e) => edit({ busyMode: e.target.value as Draft["busyMode"] })}
          >
            {BUSY_MODES.map((mode) => (
              <option key={mode.value} value={mode.value}>
                {mode.label}
              </option>
            ))}
          </select>
        </label>
        <p className="hint">{BUSY_MODES.find((mode) => mode.value === draft.busyMode)?.hint}</p>
        <label className="field">
          <span className="label">Most runs at the same time in one session</span>
          <input
            className="input"
            type="number"
            min={1}
            max={options.limits.max_parallel_runs ?? 10}
            value={draft.maxParallelRuns}
            readOnly={readOnly}
            onChange={(e) => edit({ maxParallelRuns: e.target.value })}
          />
        </label>
        <p className="hint">
          Applies to the mode Run in parallel, to /bg, and to the Run in parallel button of the chat.
        </p>
      </section>

      <section className={`card ${styles.section}`}>
        <h2 className={styles.heading}>Soul</h2>
        <p className="hint">
          The standing instructions of the agent: who it is and how it works. It is sent with every
          session. Do not put secrets here.
        </p>
        <label className="field">
          <span className="sr-only">Soul</span>
          <textarea
            className={`input ${styles.soul}`}
            rows={10}
            value={draft.soul}
            readOnly={readOnly}
            onChange={(e) => edit({ soul: e.target.value })}
          />
        </label>
        <p className={`hint ${draft.soul.length > soulMax ? styles.over : ""}`}>
          {draft.soul.length.toLocaleString()} of {soulMax.toLocaleString()} characters
        </p>
      </section>

      <section className={`card ${styles.section}`}>
        <h2 className={styles.heading}>Tools</h2>
        <p className="hint">The groups of tools that the agent can use. Risky tools still ask for approval.</p>
        <ul className={styles.toolsets}>
          {options.toolsets.map((set) => (
            <li key={set.name}>
              <label className={styles.check}>
                <input
                  type="checkbox"
                  checked={draft.toolsets.includes(set.name)}
                  disabled={readOnly}
                  onChange={() => edit({ toolsets: toggle(draft.toolsets, set.name) })}
                />
                <span>
                  <span className={styles.checkName}>{set.name}</span>
                  <span className="hint">{set.tools.join(", ")}</span>
                </span>
              </label>
            </li>
          ))}
        </ul>
      </section>

      <section className={`card ${styles.section}`}>
        <div className={styles.row}>
          <h2 className={styles.heading}>Sub-agents</h2>
          {runtime.subagents && !readOnly ? (
            <button
              type="button"
              className="btn btn-small"
              disabled={draft.subagents.length + draft.librarySubagents.length >= maxSubagents}
              onClick={() => edit({ subagents: [...draft.subagents, newSubagent()] })}
            >
              Add a sub-agent
            </button>
          ) : null}
        </div>
        {!runtime.subagents && draft.subagents.length === 0 ? (
          <p className="hint">The runtime of this agent does not support sub-agents.</p>
        ) : draft.subagents.length === 0 ? (
          <p className="hint">
            No sub-agents. A sub-agent is a helper with its own prompt and tools that the agent can hand work to.
          </p>
        ) : (
          <ul className={styles.subagents}>
            {draft.subagents.map((sub, index) => {
              const choices = [...new Set([...draft.toolsets, ...sub.toolsets])];
              const id = `sub-${sub.uid}`;
              return (
                <li key={sub.uid} className={styles.subagent}>
                  <div className={styles.row}>
                    <h3 className={styles.subTitle}>{sub.name.trim() || `Sub-agent ${index + 1}`}</h3>
                    {!readOnly ? (
                      <button
                        type="button"
                        className="btn btn-small btn-danger"
                        onClick={() => edit({ subagents: draft.subagents.filter((s) => s.uid !== sub.uid) })}
                      >
                        Remove
                      </button>
                    ) : null}
                  </div>
                  <label className="field">
                    <span className="label">Name</span>
                    <input
                      className="input"
                      value={sub.name}
                      maxLength={options.limits.subagent_name_max_chars}
                      readOnly={readOnly}
                      onChange={(e) => updateSub(sub.uid, { name: e.target.value })}
                    />
                    <span className="hint">Lowercase letters, digits, and hyphens, for example code-reviewer.</span>
                  </label>
                  <label className="field">
                    <span className="label">Description</span>
                    <input
                      className="input"
                      value={sub.description}
                      maxLength={options.limits.subagent_description_max_chars}
                      readOnly={readOnly}
                      onChange={(e) => updateSub(sub.uid, { description: e.target.value })}
                    />
                    <span className="hint">When the agent should use this sub-agent.</span>
                  </label>
                  <label className="field">
                    <span className="label">Prompt</span>
                    <textarea
                      className={`input ${styles.prompt}`}
                      rows={5}
                      value={sub.prompt}
                      maxLength={options.limits.subagent_prompt_max_chars}
                      readOnly={readOnly}
                      onChange={(e) => updateSub(sub.uid, { prompt: e.target.value })}
                    />
                  </label>
                  <fieldset className={styles.fieldset} aria-describedby={`${id}-tools`}>
                    <legend className="label">Tools</legend>
                    {choices.length === 0 ? (
                      <p className="hint" id={`${id}-tools`}>
                        Turn on a tool group for the agent first.
                      </p>
                    ) : (
                      <div className={styles.chips} id={`${id}-tools`}>
                        {choices.map((name) => (
                          <label key={name} className={styles.chip}>
                            <input
                              type="checkbox"
                              checked={sub.toolsets.includes(name)}
                              disabled={readOnly}
                              onChange={() => updateSub(sub.uid, { toolsets: toggle(sub.toolsets, name) })}
                            />
                            <span>
                              {name}
                              {draft.toolsets.includes(name) ? "" : " (off for the agent)"}
                            </span>
                          </label>
                        ))}
                      </div>
                    )}
                  </fieldset>
                  <div className={styles.pair}>
                    <label className="field">
                      <span className="label">Model</span>
                      <select
                        className="input"
                        value={sub.model}
                        disabled={readOnly}
                        onChange={(e) => updateSub(sub.uid, { model: e.target.value })}
                      >
                        <option value="">Same as the agent</option>
                        {withCurrent(runtime.models, sub.model).map((m) => (
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
                        max={options.limits.subagent_max_turns}
                        placeholder="Default"
                        value={sub.maxTurns}
                        readOnly={readOnly}
                        onChange={(e) => updateSub(sub.uid, { maxTurns: e.target.value })}
                      />
                    </label>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <ProfileLibrarySubagents
        orgId={orgId}
        workspaceId={profile.workspace_id ?? workspaceId}
        ids={draft.librarySubagents}
        onChange={(ids) => edit({ librarySubagents: ids })}
        readOnly={readOnly}
        supported={runtime.subagents}
        inlineNames={draft.subagents.map((s) => s.name)}
        toolsets={draft.toolsets}
        total={draft.subagents.length + draft.librarySubagents.length}
        max={maxSubagents}
      />

      {children}

      {!readOnly ? (
        <div className={styles.bar}>
          <div className={styles.barText} aria-live="polite">
            {error ? (
              <span className={styles.errorText} role="alert">
                {error}
              </span>
            ) : problem && dirty ? (
              <span className={styles.errorText}>{problem}</span>
            ) : saved ? (
              <span>Saved.</span>
            ) : dirty ? (
              <span>Unsaved changes</span>
            ) : null}
          </div>
          <button
            type="button"
            className="btn"
            disabled={!dirty || saving}
            onClick={() => {
              setDraft(toDraft(profile));
              setError(null);
            }}
          >
            Discard
          </button>
          <button type="submit" className="btn btn-primary" disabled={!dirty || saving || problem !== null}>
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      ) : null}
    </form>
  );
}
