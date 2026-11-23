"use client";

// The Harness section (M7.10): the runtime, the model, the reasoning effort, the busy mode, and
// the run budget of an agent. It writes through `PATCH /orgs/{org_id}/agent-profiles/{id}`.

import Link from "next/link";
import { useMemo, useState } from "react";
import { type AgentProfile, errorText } from "@/lib/api";
import { canManageAgents } from "@/lib/console";
import {
  type HarnessDraft,
  capHint,
  harnessPatch,
  reasoningLabel,
  toHarnessDraft,
  validateHarness,
} from "@/lib/harness";
import { BUSY_MODES, runtimeLabel, runtimeOption, withCurrent, withRuntime } from "@/lib/profile";
import { useApi } from "../../AuthProvider";
import { useData } from "../../DataProvider";
import { NewAgentForm } from "../../NewAgentForm";
import admin from "../../Admin.module.css";
import styles from "../../Page.module.css";
import { AgentProfileEditor, SectionHeader, useAgentChoice } from "../parts";

export function HarnessSection() {
  const choice = useAgentChoice();
  const { org, workspaceId, setProfileId } = choice;
  const { reloadMe } = useData();
  const [creating, setCreating] = useState(false);
  const mayCreate = canManageAgents(org);

  return (
    <>
      <SectionHeader
        id="harness"
        actions={
          mayCreate && !creating ? (
            <button type="button" className="btn btn-primary btn-small" onClick={() => setCreating(true)}>
              New agent
            </button>
          ) : null
        }
        meta={mayCreate ? null : "Only an org admin with the right to manage agents can change these settings."}
      />
      {creating ? (
        <section className={`card ${styles.card}`} aria-labelledby="harness-new">
          <h2 id="harness-new" className={styles.heading}>
            New agent
          </h2>
          <NewAgentForm
            org={org}
            workspaceId={workspaceId}
            onCreated={(profile) => {
              reloadMe();
              setProfileId(profile.id);
              setCreating(false);
            }}
            onCancel={() => setCreating(false)}
          />
        </section>
      ) : null}
      <AgentProfileEditor
        choice={choice}
        emptyText={
          mayCreate
            ? "This org has no agent yet. Make the first agent with the button New agent."
            : "This org has no agent yet. Ask an org admin to make one."
        }
      >
        {({ profile, readOnly, onSaved }) => (
          <HarnessForm orgId={org.id} profile={profile} readOnly={readOnly} onSaved={onSaved} />
        )}
      </AgentProfileEditor>
    </>
  );
}

function HarnessForm({
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
  const { options } = profile;
  const [draft, setDraft] = useState<HarnessDraft>(() => toHarnessDraft(profile));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const patch = useMemo(() => harnessPatch(profile, draft), [profile, draft]);
  const dirty = Object.keys(patch).length > 0;
  const problem = useMemo(() => validateHarness(draft, options.budget_caps, options.limits.max_parallel_runs), [draft, options.budget_caps, options.limits.max_parallel_runs]);
  const runtime = runtimeOption(options, profile.runtime, draft.runtime);
  const runtimes = withCurrent(
    options.runtimes.map((r) => r.name),
    draft.runtime,
  );
  const models = withCurrent(runtime.models, draft.model);
  const caps = options.budget_caps;

  const edit = (change: Partial<HarnessDraft>) => {
    setDraft((d) => ({ ...d, ...change }));
    setSaved(false);
  };
  const editBudget = (change: Partial<HarnessDraft["budget"]>) => {
    setDraft((d) => ({ ...d, budget: { ...d.budget, ...change } }));
    setSaved(false);
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!dirty || problem !== null || readOnly) return;
    const submitted = draft;
    setSaving(true);
    setError(null);
    try {
      const result = await api.updateAgentProfile(orgId, profile.id, patch);
      onSaved(result);
      // An edit made while the request ran stays.
      setDraft((current) => (current === submitted ? toHarnessDraft(result) : current));
      setSaved(true);
    } catch (e) {
      setError(errorText(e, "The changes were not saved."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className={styles.form} onSubmit={save} aria-label={`Harness of ${profile.name}`}>
      <section className={`card ${styles.card}`} aria-labelledby="harness-model">
        <div className={styles.cardHead}>
          <h2 id="harness-model" className={styles.heading}>
            Runtime and model
          </h2>
          <Link href={`/agents/${profile.id}`} className="btn btn-quiet btn-small">
            Soul and history
          </Link>
        </div>
        <p className="hint">
          Changes apply to new sessions. The soul, the tool groups, and the history of the agent are on the agent page.
        </p>
        <label className="field">
          <span className="label">Runtime</span>
          <select
            className="input"
            value={draft.runtime}
            disabled={readOnly}
            onChange={(e) => {
              setDraft((d) => withRuntime(d, profile, e.target.value));
              setSaved(false);
            }}
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
        <label className="field">
          <span className="label">Reasoning effort</span>
          <select
            className="input"
            value={draft.reasoning}
            disabled={readOnly}
            onChange={(e) => edit({ reasoning: e.target.value as HarnessDraft["reasoning"] })}
          >
            <option value="">{reasoningLabel("")}</option>
            {options.reasoning_efforts.map((effort) => (
              <option key={effort} value={effort}>
                {reasoningLabel(effort)}
              </option>
            ))}
          </select>
          <span className="hint">
            More effort gives better answers to hard tasks. It costs more time and tokens. New agents
            start at High, which costs about twice the model default.
          </span>
        </label>
      </section>

      {options.busy_modes !== undefined ? (
        <section className={`card ${styles.card}`} aria-labelledby="harness-busy">
          <h2 id="harness-busy" className={styles.heading}>
            Busy mode
          </h2>
          <p className="hint">What happens when a message comes while the agent works on a run.</p>
          <fieldset className={admin.choices} disabled={readOnly}>
            <legend className="sr-only">Busy mode</legend>
            {BUSY_MODES.filter((m) => options.busy_modes?.includes(m.value)).map((mode) => (
              <label key={mode.value} className={admin.choice}>
                <input
                  type="radio"
                  name="busy-mode"
                  checked={draft.busyMode === mode.value}
                  onChange={() => edit({ busyMode: mode.value })}
                />
                <span className={admin.choiceText}>
                  <strong>
                    {mode.label}
                    {mode.value === "steer" ? " (default)" : ""}
                  </strong>
                  <span className="hint">{mode.hint}</span>
                </span>
              </label>
            ))}
          </fieldset>
          <label className="field">
            <span className="label">Most runs at the same time in one session</span>
            <input
              className="input"
              type="number"
              inputMode="numeric"
              min={1}
              max={options.limits.max_parallel_runs ?? 10}
              value={draft.maxParallelRuns}
              readOnly={readOnly}
              onChange={(e) => edit({ maxParallelRuns: e.target.value })}
            />
            <span className="hint">
              Applies to the mode Run in parallel, to /bg, and to the Run in parallel button of the chat.
            </span>
          </label>
        </section>
      ) : null}

      <section className={`card ${styles.card}`} aria-labelledby="harness-budget">
        <h2 id="harness-budget" className={styles.heading}>
          Run budget
        </h2>
        <p className="hint">The most that one run can use. A run that reaches a limit stops.</p>
        <div className={admin.grid}>
          <label className="field">
            <span className="label">Max turns</span>
            <input
              className="input"
              type="number"
              inputMode="numeric"
              min={1}
              step={1}
              value={draft.budget.maxTurns}
              readOnly={readOnly}
              onChange={(e) => editBudget({ maxTurns: e.target.value })}
            />
            <span className="hint">{capHint(caps.max_turns, "turns")}</span>
          </label>
          <label className="field">
            <span className="label">Max cost (US dollars)</span>
            <input
              className="input"
              type="number"
              inputMode="decimal"
              min={0}
              step="any"
              value={draft.budget.maxCostUsd}
              readOnly={readOnly}
              onChange={(e) => editBudget({ maxCostUsd: e.target.value })}
            />
            <span className="hint">{capHint(caps.max_cost_usd, "US dollars")}</span>
          </label>
          <label className="field">
            <span className="label">Max wall time (minutes)</span>
            <input
              className="input"
              type="number"
              inputMode="decimal"
              min={0}
              step="any"
              value={draft.budget.maxWallMinutes}
              readOnly={readOnly}
              onChange={(e) => editBudget({ maxWallMinutes: e.target.value })}
            />
            <span className="hint">
              {capHint(caps.max_wall_time_s === null ? null : caps.max_wall_time_s / 60, "minutes")}
            </span>
          </label>
          <label className="field">
            <span className="label">Max tokens</span>
            <input
              className="input"
              type="number"
              inputMode="numeric"
              min={1}
              step={1}
              value={draft.budget.maxTokens}
              readOnly={readOnly}
              onChange={(e) => editBudget({ maxTokens: e.target.value })}
            />
            <span className="hint">Empty means no limit.</span>
          </label>
        </div>
      </section>

      {readOnly ? (
        <p className="alert" role="note">
          You can read these settings but not change them. Only an org admin with the right to manage agents can.
        </p>
      ) : (
        <>
          {error ? (
            <p className="alert alert-error" role="alert">
              {error}
            </p>
          ) : problem && dirty ? (
            <p className="alert alert-warning">{problem}</p>
          ) : saved ? (
            <p className="alert alert-success" role="status">
              Saved. The change applies to new sessions.
            </p>
          ) : null}
          <div className={styles.actions}>
            <button type="submit" className="btn btn-primary" disabled={!dirty || saving || problem !== null}>
              {saving ? "Saving…" : "Save changes"}
            </button>
            <button
              type="button"
              className="btn"
              disabled={!dirty || saving}
              onClick={() => {
                setDraft(toHarnessDraft(profile));
                setError(null);
              }}
            >
              Discard
            </button>
          </div>
        </>
      )}
    </form>
  );
}
