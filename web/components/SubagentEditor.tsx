"use client";

// The form of a library sub-agent (roadmap M7.4): create, edit, and delete. The form takes the
// org and the workspace as props and does its own saving. A reader without the right to edit
// sees the same fields, read-only.

import { useMemo, useRef, useState } from "react";
import { errorText } from "@/lib/api";
import { runtimeLabel } from "@/lib/profile";
import {
  type LibraryAgent,
  type LibraryOptions,
  type SubagentDraft,
  type SubagentScope,
  diffDraft,
  draftProblem,
  emptyDraft,
  modelLabel,
  SCOPE_HINTS,
  SCOPE_LABELS,
  SCOPE_ORDER,
  toCreateBody,
  toDraft,
} from "@/lib/subagents";
import { useApi } from "./AuthProvider";
import { useFocusWhen } from "./useFocusWhen";
import page from "./Page.module.css";
import styles from "./AgentSettings.module.css";

/** The models of the options, with `current` in the list so that a select can show it. */
function modelGroups(options: LibraryOptions, current: string) {
  const groups = options.models.map((g) => ({ runtime: g.runtime, models: g.models }));
  const known = groups.some((g) => g.models.includes(current));
  return { groups, extra: current !== "" && !known ? current : null };
}

export function SubagentEditor({
  orgId,
  workspaceId,
  options,
  writableScopes,
  agent,
  onSaved,
  onDeleted,
  onCancel,
}: {
  orgId: string;
  /** The workspace of a new workspace agent. */
  workspaceId: string | null;
  options: LibraryOptions;
  writableScopes: SubagentScope[];
  /** The agent to edit, or `null` to create one. */
  agent: LibraryAgent | null;
  onSaved: (agent: LibraryAgent, created: boolean) => void;
  onDeleted?: () => void;
  onCancel: () => void;
}) {
  const api = useApi();
  const creating = agent === null;
  const readOnly = !creating && !agent.can_edit;
  const [draft, setDraft] = useState<SubagentDraft>(() => (agent ? toDraft(agent) : emptyDraft()));
  const [scope, setScope] = useState<SubagentScope>(
    SCOPE_ORDER.find((s) => writableScopes.includes(s)) ?? "user",
  );
  const [busy, setBusy] = useState<"save" | "delete" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const nameField = useRef<HTMLInputElement>(null);
  useFocusWhen(creating, nameField);

  const { limits } = options;
  const patch = useMemo(() => (agent ? diffDraft(agent, draft) : null), [agent, draft]);
  const dirty = creating ? true : Object.keys(patch ?? {}).length > 0;
  const problem = draftProblem(draft, limits);
  const scopeProblem =
    creating && scope === "workspace" && workspaceId === null ? "Choose a workspace first." : null;
  const { groups, extra } = modelGroups(options, draft.model);

  const edit = (change: Partial<SubagentDraft>) => {
    setDraft((d) => ({ ...d, ...change }));
    setTouched(true);
  };
  const toggle = (name: string) =>
    edit({
      toolsets: draft.toolsets.includes(name)
        ? draft.toolsets.filter((n) => n !== name)
        : [...draft.toolsets, name],
    });

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (readOnly || problem !== null || scopeProblem !== null || !dirty) return;
    setBusy("save");
    setError(null);
    try {
      const saved = agent
        ? await api.updateSubagent(orgId, agent.id, patch ?? {})
        : await api.createSubagent(orgId, toCreateBody(draft, scope, workspaceId));
      onSaved(saved, creating);
    } catch (e) {
      setError(errorText(e, "The sub-agent was not saved."));
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    if (agent === null) return;
    setBusy("delete");
    setError(null);
    try {
      await api.deleteSubagent(orgId, agent.id);
      onDeleted?.();
    } catch (e) {
      // A 409 names the agent profiles that use the agent.
      setConfirmingDelete(false);
      setError(errorText(e, "The sub-agent was not deleted."));
    } finally {
      setBusy(null);
    }
  };

  const shownProblem = touched ? (problem ?? scopeProblem) : null;
  const id = agent?.id ?? "new";

  return (
    <form className={page.form} onSubmit={save} aria-label={creating ? "New sub-agent" : `Edit ${agent.name}`}>
      {readOnly ? (
        <p className="alert" role="note">
          You cannot change this sub-agent. {SCOPE_HINTS[agent.scope]}
        </p>
      ) : null}
      <label className="field">
        <span className="label">Name</span>
        <input
          ref={nameField}
          className="input"
          value={draft.name}
          maxLength={limits.name_max_chars}
          readOnly={readOnly}
          autoComplete="off"
          onChange={(e) => edit({ name: e.target.value })}
        />
        <span className="hint">Lowercase letters, digits, and hyphens, for example code-reviewer.</span>
      </label>
      <label className="field">
        <span className="label">Description</span>
        <input
          className="input"
          value={draft.description}
          maxLength={limits.description_max_chars}
          readOnly={readOnly}
          onChange={(e) => edit({ description: e.target.value })}
        />
        <span className="hint">Tells the agent when to use this sub-agent.</span>
      </label>
      <label className="field">
        <span className="label">Prompt</span>
        <textarea
          className={`input ${page.editor}`}
          rows={8}
          value={draft.prompt}
          maxLength={limits.prompt_max_chars}
          readOnly={readOnly}
          spellCheck={false}
          onChange={(e) => edit({ prompt: e.target.value })}
        />
        <span className={`hint ${draft.prompt.length > limits.prompt_max_chars ? styles.over : ""}`}>
          {draft.prompt.length.toLocaleString()} of {limits.prompt_max_chars.toLocaleString()} characters
        </span>
      </label>
      <fieldset className={styles.fieldset} aria-describedby={`${id}-tools`}>
        <legend className="label">Tools</legend>
        <p className="hint" id={`${id}-tools`}>
          The tool groups that the sub-agent can use. An agent that uses it must have the same groups.
        </p>
        <div className={styles.chips}>
          {options.toolsets.map((set) => (
            <label key={set.name} className={styles.chip} title={set.tools.join(", ")}>
              <input
                type="checkbox"
                checked={draft.toolsets.includes(set.name)}
                disabled={readOnly}
                onChange={() => toggle(set.name)}
              />
              <span>{set.name}</span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className={styles.pair}>
        <label className="field">
          <span className="label">Model</span>
          <select
            className="input"
            value={draft.model}
            disabled={readOnly}
            onChange={(e) => edit({ model: e.target.value })}
          >
            <option value="">{modelLabel(null)}</option>
            {extra !== null ? <option value={extra}>{extra}</option> : null}
            {groups.map((g) => (
              <optgroup key={g.runtime} label={runtimeLabel(g.runtime)}>
                {g.models.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          <span className="hint">A model of a runtime only works with agents of that runtime.</span>
        </label>
        <label className="field">
          <span className="label">Max turns</span>
          <input
            className="input"
            type="number"
            inputMode="numeric"
            min={1}
            max={limits.max_turns}
            placeholder="Default"
            value={draft.maxTurns}
            readOnly={readOnly}
            onChange={(e) => edit({ maxTurns: e.target.value })}
          />
        </label>
      </div>
      <label className="field">
        <span className="label">Max cost (USD)</span>
        <input
          className="input"
          inputMode="decimal"
          placeholder="No limit"
          value={draft.maxCost}
          readOnly={readOnly}
          autoComplete="off"
          onChange={(e) => edit({ maxCost: e.target.value })}
        />
        <span className="hint">The most that one call of the sub-agent can cost. For example 0.50.</span>
      </label>
      {creating ? (
        <label className="field">
          <span className="label">Who can use it</span>
          <select
            className="input"
            value={scope}
            onChange={(e) => setScope(e.target.value as SubagentScope)}
            aria-describedby={`${id}-scope`}
          >
            {SCOPE_ORDER.filter((s) => writableScopes.includes(s)).map((s) => (
              <option key={s} value={s}>
                {SCOPE_LABELS[s]}
              </option>
            ))}
          </select>
          <span className="hint" id={`${id}-scope`}>
            {SCOPE_HINTS[scope]}
          </span>
        </label>
      ) : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : shownProblem ? (
        <p className="alert alert-error" role="status">
          {shownProblem}
        </p>
      ) : null}
      {readOnly ? null : (
        <div className={page.actions}>
          <button type="submit" className="btn btn-primary btn-small" disabled={busy !== null || !dirty}>
            {busy === "save" ? "Saving…" : creating ? "Create" : "Save"}
          </button>
          {creating ? (
            <button type="button" className="btn btn-quiet btn-small" disabled={busy !== null} onClick={onCancel}>
              Cancel
            </button>
          ) : dirty ? (
            <button
              type="button"
              className="btn btn-quiet btn-small"
              disabled={busy !== null}
              onClick={() => {
                setDraft(toDraft(agent));
                setTouched(false);
                setError(null);
              }}
            >
              Discard changes
            </button>
          ) : null}
          {!creating && onDeleted ? (
            confirmingDelete ? (
              <>
                <button
                  type="button"
                  className="btn btn-danger btn-small"
                  disabled={busy !== null}
                  onClick={() => void remove()}
                >
                  {busy === "delete" ? "Deleting…" : "Confirm delete"}
                </button>
                <button
                  type="button"
                  className="btn btn-quiet btn-small"
                  disabled={busy !== null}
                  onClick={() => setConfirmingDelete(false)}
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                className="btn btn-danger btn-small"
                disabled={busy !== null}
                onClick={() => {
                  setError(null);
                  setConfirmingDelete(true);
                }}
              >
                Delete
              </button>
            )
          ) : null}
        </div>
      )}
    </form>
  );
}
