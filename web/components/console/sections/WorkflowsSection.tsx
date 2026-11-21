"use client";

// The Workflows section (M7.10): durable workflows of steps, approvals, and waits (Section 12.3).
// A list, an editor with a check, and a detail with the start, the approval, and the instances.

import Link from "next/link";
import { useCallback, useState } from "react";
import {
  type Workflow,
  type WorkflowDetail,
  type WorkflowInstance,
  type WorkflowInstanceDetail,
  type WorkflowValidation,
  errorText,
} from "@/lib/api";
import { can } from "@/lib/console";
import { formatWhen, statusLabel, statusTone } from "@/lib/jobs";
import { parseJsonObject, prettyJson } from "@/lib/jsonText";
import {
  MAX_INPUTS_BYTES,
  WORKFLOW_TEMPLATE,
  canCancelInstance,
  canStart,
  instanceStatusLabel,
  instanceStatusTone,
  pauseAction,
  validateYamlText,
  validationNotes,
} from "@/lib/workflows";
import { useApi } from "../../AuthProvider";
import { ErrorState, LoadingState, ToneBadge } from "../../ui";
import { useLoad } from "../../useLoad";
import admin from "../../Admin.module.css";
import styles from "../../Page.module.css";
import { useConsole } from "../Console";
import { SectionHeader, useAgentChoice } from "../parts";
import own from "../TriggerScreens.module.css";

type View = { kind: "list" } | { kind: "new" } | { kind: "edit"; id: string } | { kind: "detail"; id: string };

export function WorkflowsSection() {
  const api = useApi();
  const { org } = useConsole();
  const [view, setView] = useState<View>({ kind: "list" });
  const mayCreate = can(org, "workflows.create");
  const load = useCallback(() => api.listWorkflows(org.id), [api, org.id]);
  const { data: workflows, error, reload } = useLoad(load, "Could not load the workflows.");

  return (
    <>
      <SectionHeader
        id="workflows"
        actions={
          view.kind === "list" && mayCreate ? (
            <button type="button" className="btn btn-primary btn-small" onClick={() => setView({ kind: "new" })}>
              New workflow
            </button>
          ) : null
        }
        meta={mayCreate ? "You see the workflows that you own." : "Your role cannot create a workflow. Ask an org admin."}
      />
      {view.kind === "new" || view.kind === "edit" ? (
        <WorkflowEditor
          key={view.kind === "edit" ? view.id : "new"}
          workflowId={view.kind === "edit" ? view.id : null}
          onCancel={() => setView(view.kind === "edit" ? { kind: "detail", id: view.id } : { kind: "list" })}
          onSaved={(saved) => {
            reload();
            setView({ kind: "detail", id: saved.id });
          }}
        />
      ) : view.kind === "detail" ? (
        <WorkflowDetailView
          key={view.id}
          workflowId={view.id}
          onBack={() => setView({ kind: "list" })}
          onEdit={() => setView({ kind: "edit", id: view.id })}
          onChanged={reload}
        />
      ) : error ? (
        <ErrorState title="The workflows did not load" message={error} onRetry={reload} />
      ) : workflows === null ? (
        <LoadingState label="Loading the workflows" />
      ) : workflows.filter((w) => w.status !== "removed").length === 0 ? (
        <div className="state">
          <h2>No workflows</h2>
          <p>A workflow is a saved list of steps. It can run an agent, call a tool, wait for an approval, or wait for a time.</p>
          {mayCreate ? (
            <button type="button" className="btn btn-primary" onClick={() => setView({ kind: "new" })}>
              New workflow
            </button>
          ) : null}
        </div>
      ) : (
        <ul className={styles.list} aria-label="Workflows">
          {workflows
            .filter((w) => w.status !== "removed")
            .map((w) => (
              <WorkflowRow key={w.id} workflow={w} onOpen={() => setView({ kind: "detail", id: w.id })} />
            ))}
        </ul>
      )}
    </>
  );
}

function WorkflowRow({ workflow, onOpen }: { workflow: Workflow; onOpen: () => void }) {
  return (
    <li className={`card ${styles.card}`}>
      <div className={styles.row}>
        <div className={styles.name}>
          {workflow.name}
          <ToneBadge tone={statusTone(workflow.status)}>{statusLabel(workflow.status)}</ToneBadge>
          <span className="badge">Version {workflow.version}</span>
        </div>
        <div className={styles.actions}>
          <button type="button" className="btn btn-small" onClick={onOpen} aria-label={`Open ${workflow.name}`}>
            Open
          </button>
        </div>
      </div>
      {workflow.description ? <p className={styles.text}>{workflow.description}</p> : null}
      <p className={styles.text}>
        {workflow.next_fire_at ? `Next run: ${formatWhen(workflow.next_fire_at)}` : "No run is planned."}
      </p>
    </li>
  );
}

// -- editor ---------------------------------------------------------------------------------------

function WorkflowEditor({
  workflowId,
  onCancel,
  onSaved,
}: {
  /** The workflow to change, or `null` for a new one. */
  workflowId: string | null;
  onCancel: () => void;
  onSaved: (workflow: WorkflowDetail) => void;
}) {
  const api = useApi();
  const { org } = useConsole();
  const load = useCallback(
    () => (workflowId === null ? Promise.resolve(null) : api.getWorkflow(org.id, workflowId)),
    [api, org.id, workflowId],
  );
  const { data: existing, error, reload } = useLoad(load, "Could not load the workflow.");
  const loading = workflowId !== null && existing === null;

  if (error) return <ErrorState title="The workflow did not load" message={error} onRetry={reload} />;
  if (loading) return <LoadingState label="Loading the workflow" />;
  return <WorkflowEditorForm existing={existing} onCancel={onCancel} onSaved={onSaved} />;
}

function WorkflowEditorForm({
  existing,
  onCancel,
  onSaved,
}: {
  existing: WorkflowDetail | null;
  onCancel: () => void;
  onSaved: (workflow: WorkflowDetail) => void;
}) {
  const api = useApi();
  const { org, workspaceId, profiles, profileId: firstProfile } = useAgentChoice();
  const isNew = existing === null;
  const [yaml, setYaml] = useState(existing?.source ?? WORKFLOW_TEMPLATE);
  const [agentId, setAgentId] = useState(firstProfile ?? "");
  // The result of a check belongs to the YAML that it checked. It shows only while the text is the same.
  const [checked, setChecked] = useState<{
    yaml: string;
    result: WorkflowValidation | null;
    error: string | null;
  } | null>(null);
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const problem = validateYamlText(yaml);
  const unchanged = !isNew && yaml === (existing.source ?? "");

  const shown = checked !== null && checked.yaml === yaml ? checked : null;

  const runCheck = async () => {
    const submitted = yaml;
    if (problem) {
      setChecked({ yaml: submitted, result: null, error: problem });
      return;
    }
    setChecking(true);
    setChecked(null);
    try {
      setChecked({ yaml: submitted, result: await api.validateWorkflow(submitted), error: null });
    } catch (e) {
      setChecked({ yaml: submitted, result: null, error: errorText(e, "The check did not run.") });
    } finally {
      setChecking(false);
    }
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (problem) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const saved = isNew
        ? await api.createWorkflow(org.id, {
            yaml,
            workspace_id: workspaceId,
            ...(agentId === "" ? {} : { agent_profile_id: agentId }),
          })
        : await api.updateWorkflow(org.id, existing.id, { yaml });
      onSaved(saved);
    } catch (e) {
      setError(errorText(e, "The workflow was not saved."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className={`card ${styles.card}`} onSubmit={save} aria-labelledby="workflow-form">
      <div className={styles.cardHead}>
        <h2 id="workflow-form" className={styles.heading}>
          {isNew ? "New workflow" : `Edit ${existing.name}`}
        </h2>
        <button type="button" className="btn btn-quiet btn-small" onClick={onCancel} disabled={saving}>
          {isNew ? "Back to the list" : "Cancel"}
        </button>
      </div>
      <p className="hint">
        Write the workflow in YAML: a name, optional inputs and triggers, and the steps. A step has an id and one of
        tool, agent, approval, branch, parallel, wait, or workflow. Press Check to see the result before you save.
      </p>
      {isNew ? (
        <label className="field">
          <span className="label">Agent</span>
          <select className="input" value={agentId} disabled={saving} onChange={(e) => setAgentId(e.target.value)}>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <span className="hint">The agent runs the agent steps of the workflow.</span>
        </label>
      ) : null}
      <label className="field">
        <span className="label">Workflow (YAML)</span>
        <textarea
          className={`input ${styles.editor}`}
          rows={16}
          value={yaml}
          spellCheck={false}
          disabled={saving}
          onChange={(e) => setYaml(e.target.value)}
        />
      </label>
      {shown?.error ? (
        <p className="alert alert-error" role="alert">
          {shown.error}
        </p>
      ) : null}
      {shown?.result ? <CheckResult check={shown.result} /> : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button type="button" className="btn" disabled={checking || saving} onClick={runCheck}>
          {checking ? "Checking…" : "Check"}
        </button>
        <button type="submit" className="btn btn-primary" disabled={saving || unchanged}>
          {saving ? "Saving…" : isNew ? "Create the workflow" : "Save a new version"}
        </button>
      </div>
    </form>
  );
}

function CheckResult({ check }: { check: WorkflowValidation }) {
  const notes = validationNotes(check);
  return (
    <div className={notes.length > 0 ? "alert alert-warning" : "alert alert-success"} role="status">
      <p>
        <strong>The workflow is valid.</strong>{" "}
        {check.next_fire_at ? `Next run: ${formatWhen(check.next_fire_at)}.` : "It has no schedule, so it runs when you start it."}
      </p>
      {notes.length > 0 ? (
        <ul>
          {notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : (
        <p>It needs no approval.</p>
      )}
    </div>
  );
}

// -- detail ---------------------------------------------------------------------------------------

function WorkflowDetailView({
  workflowId,
  onBack,
  onEdit,
  onChanged,
}: {
  workflowId: string;
  onBack: () => void;
  onEdit: () => void;
  onChanged: () => void;
}) {
  const api = useApi();
  const { org } = useConsole();
  const load = useCallback(() => api.getWorkflow(org.id, workflowId), [api, org.id, workflowId]);
  const { data: workflow, setData: setWorkflow, error, reload } = useLoad(load, "Could not load the workflow.");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [openInstance, setOpenInstance] = useState<string | null>(null);

  if (error) {
    return (
      <>
        <div className={styles.actions}>
          <button type="button" className="btn btn-quiet btn-small" onClick={onBack}>
            Back to the list
          </button>
        </div>
        <ErrorState title="The workflow did not load" message={error} onRetry={reload} />
      </>
    );
  }
  if (workflow === null) return <LoadingState label="Loading the workflow" />;

  const act = async (action: () => Promise<WorkflowDetail | null>, failure: string, done: string) => {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const updated = await action();
      if (updated) setWorkflow(updated);
      onChanged();
      setNotice(done);
    } catch (e) {
      setActionError(errorText(e, failure));
    } finally {
      setBusy(false);
    }
  };

  const pause = pauseAction(workflow.status);
  const triggerText = (workflow.triggers ?? []).map((t) => t.text ?? "A schedule").join("; ");
  const instances = workflow.instances ?? [];

  const remove = async () => {
    if (!window.confirm(`Delete the workflow ${workflow.name}?`)) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.deleteWorkflow(org.id, workflow.id);
      onChanged();
      onBack();
    } catch (e) {
      setActionError(errorText(e, "The workflow was not deleted."));
      setBusy(false);
    }
  };

  return (
    <>
      <div className={styles.actions}>
        <button type="button" className="btn btn-quiet btn-small" onClick={onBack}>
          Back to the list
        </button>
      </div>
      <section className={`card ${styles.card}`} aria-labelledby="workflow-overview">
        <div className={styles.cardHead}>
          <h2 id="workflow-overview" className={styles.heading}>
            {workflow.name}
          </h2>
          <span className={admin.chips}>
            <ToneBadge tone={statusTone(workflow.status)}>{statusLabel(workflow.status)}</ToneBadge>
            <span className="badge">Version {workflow.version}</span>
          </span>
        </div>
        {workflow.description ? <p className={styles.text}>{workflow.description}</p> : null}
        <dl className={own.facts}>
          <div className={own.fact}>
            <dt className={own.factLabel}>Triggers</dt>
            <dd className={own.factValue}>
              {triggerText !== ""
                ? triggerText
                : "None. The workflow runs when you start it."}
            </dd>
          </div>
          <div className={own.fact}>
            <dt className={own.factLabel}>Next run</dt>
            <dd className={own.factValue}>{workflow.next_fire_at ? formatWhen(workflow.next_fire_at) : "None"}</dd>
          </div>
          <div className={own.fact}>
            <dt className={own.factLabel}>Last run</dt>
            <dd className={own.factValue}>{workflow.last_fire_at ? formatWhen(workflow.last_fire_at) : "None"}</dd>
          </div>
        </dl>

        {workflow.needs_approval ? (
          <div className="alert alert-warning" role="note">
            <p>
              <strong>Version {workflow.version} needs your approval before it can run.</strong>
            </p>
            {workflow.approval_reasons && workflow.approval_reasons.length > 0 ? (
              <ul>
                {workflow.approval_reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            ) : null}
            <div className={styles.actions}>
              <button
                type="button"
                className="btn btn-primary btn-small"
                disabled={busy}
                onClick={() =>
                  act(
                    () => api.approveWorkflow(org.id, workflow.id, workflow.version),
                    "The approval did not work.",
                    `Version ${workflow.version} is approved.`,
                  )
                }
              >
                {busy ? "Approving…" : `Approve version ${workflow.version}`}
              </button>
            </div>
          </div>
        ) : null}

        {actionError ? (
          <p className="alert alert-error" role="alert">
            {actionError}
          </p>
        ) : notice ? (
          <p className="alert alert-success" role="status">
            {notice}
          </p>
        ) : null}
        <div className={styles.actions}>
          <button type="button" className="btn btn-small" disabled={busy} onClick={onEdit}>
            Edit
          </button>
          {pause ? (
            <button
              type="button"
              className="btn btn-small"
              disabled={busy}
              onClick={() =>
                act(
                  () => (pause === "pause" ? api.pauseWorkflow(org.id, workflow.id) : api.resumeWorkflow(org.id, workflow.id)),
                  pause === "pause" ? "The workflow was not paused." : "The workflow was not resumed.",
                  pause === "pause" ? "The workflow is paused." : "The workflow is active.",
                )
              }
            >
              {pause === "pause" ? "Pause" : "Resume"}
            </button>
          ) : null}
          <button type="button" className="btn btn-small btn-danger" disabled={busy} onClick={remove}>
            Delete
          </button>
        </div>
        {workflow.source ? (
          <details>
            <summary className={admin.summary}>Definition (YAML)</summary>
            <pre className={styles.pre}>{workflow.source}</pre>
          </details>
        ) : null}
      </section>

      <StartCard
        workflow={workflow}
        onStarted={(instance) => {
          reload();
          setOpenInstance(instance.id);
        }}
      />

      <InstancesCard
        instances={instances}
        openId={openInstance}
        onOpen={setOpenInstance}
        onRefresh={reload}
      />
      {openInstance ? (
        <InstanceView
          key={openInstance}
          instanceId={openInstance}
          onClose={() => setOpenInstance(null)}
          onChanged={reload}
        />
      ) : null}
    </>
  );
}

function StartCard({
  workflow,
  onStarted,
}: {
  workflow: WorkflowDetail;
  onStarted: (instance: WorkflowInstanceDetail) => void;
}) {
  const api = useApi();
  const { org } = useConsole();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const startable = canStart(workflow.status) && !workflow.needs_approval;

  const start = async (event: React.FormEvent) => {
    event.preventDefault();
    const parsed = parseJsonObject(text, "The inputs field", MAX_INPUTS_BYTES);
    if (!parsed.ok) {
      setError(parsed.error);
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const instance = await api.startWorkflow(org.id, workflow.id, parsed.value ?? {});
      setNotice("The workflow started.");
      onStarted(instance);
    } catch (e) {
      setError(errorText(e, "The workflow did not start."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className={`card ${styles.card}`} onSubmit={start} aria-labelledby="workflow-start">
      <h2 id="workflow-start" className={styles.heading}>
        Start now
      </h2>
      <label className="field">
        <span className="label">Inputs (optional)</span>
        <textarea
          className={`input ${styles.editor}`}
          rows={3}
          value={text}
          spellCheck={false}
          disabled={busy || !startable}
          onChange={(e) => setText(e.target.value)}
        />
        <span className="hint">A JSON object with the values of the inputs, for example {`{"topic": "news"}`}. Empty means the defaults.</span>
      </label>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      {startable && workflow.status === "paused" ? (
        <p className="hint">The workflow is paused. Its schedule does not run, but a manual start works.</p>
      ) : null}
      {!startable ? (
        <p className="hint">
          {workflow.needs_approval ? "Approve the version first." : "A workflow in this state cannot start."}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button type="submit" className="btn btn-primary btn-small" disabled={busy || !startable}>
          {busy ? "Starting…" : "Start the workflow"}
        </button>
      </div>
    </form>
  );
}

function InstancesCard({
  instances,
  openId,
  onOpen,
  onRefresh,
}: {
  instances: WorkflowInstance[];
  openId: string | null;
  onOpen: (id: string) => void;
  onRefresh: () => void;
}) {
  return (
    <section className={`card ${styles.card}`} aria-labelledby="workflow-instances">
      <div className={styles.cardHead}>
        <h2 id="workflow-instances" className={styles.heading}>
          Recent runs
        </h2>
        <button type="button" className="btn btn-small" onClick={onRefresh}>
          Refresh
        </button>
      </div>
      {instances.length === 0 ? (
        <p className="hint">This workflow has not run yet.</p>
      ) : (
        <div className={styles.scroll}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Started by</th>
                <th scope="col">Created</th>
                <th scope="col">Error</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {instances.map((i) => (
                <tr key={i.id} aria-current={openId === i.id ? "true" : undefined}>
                  <td>
                    <ToneBadge tone={instanceStatusTone(i.status)}>{instanceStatusLabel(i.status)}</ToneBadge>
                  </td>
                  <td>{i.trigger}</td>
                  <td>{formatWhen(i.created_at)}</td>
                  <td>{i.error ?? ""}</td>
                  <td>
                    <button type="button" className="btn btn-small" onClick={() => onOpen(i.id)}>
                      Steps
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function InstanceView({
  instanceId,
  onClose,
  onChanged,
}: {
  instanceId: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const api = useApi();
  const { org } = useConsole();
  const load = useCallback(() => api.getWorkflowInstance(org.id, instanceId), [api, org.id, instanceId]);
  const { data: instance, setData, error, reload } = useLoad(load, "Could not load the run.");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const cancel = async () => {
    if (!window.confirm("Cancel this run?")) return;
    setBusy(true);
    setActionError(null);
    try {
      setData(await api.cancelWorkflowInstance(org.id, instanceId));
      onChanged();
    } catch (e) {
      setActionError(errorText(e, "The run was not cancelled."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={`card ${styles.card}`} aria-labelledby="workflow-instance">
      <div className={styles.cardHead}>
        <h2 id="workflow-instance" className={styles.heading}>
          Run steps
        </h2>
        <div className={styles.actions}>
          <button type="button" className="btn btn-small" onClick={reload}>
            Refresh
          </button>
          <button type="button" className="btn btn-quiet btn-small" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
      {error ? (
        <ErrorState title="The run did not load" message={error} onRetry={reload} />
      ) : instance === null ? (
        <LoadingState label="Loading the run" />
      ) : (
        <>
          <p className={styles.text}>
            <ToneBadge tone={instanceStatusTone(instance.status)}>{instanceStatusLabel(instance.status)}</ToneBadge>{" "}
            Version {instance.version}, started by {instance.trigger} on {formatWhen(instance.created_at)}.
            {instance.finished_at ? ` Ended ${formatWhen(instance.finished_at)}.` : ""}
          </p>
          {instance.error ? (
            <p className="alert alert-error" role="note">
              {instance.error}
            </p>
          ) : null}
          {instance.inputs && Object.keys(instance.inputs).length > 0 ? (
            <details>
              <summary className={admin.summary}>Inputs</summary>
              <pre className={styles.pre}>{prettyJson(instance.inputs)}</pre>
            </details>
          ) : null}
          {instance.output !== null && instance.output !== undefined ? (
            <details>
              <summary className={admin.summary}>Output</summary>
              <pre className={styles.pre}>{typeof instance.output === "string" ? instance.output : prettyJson(instance.output)}</pre>
            </details>
          ) : null}
          {instance.steps.length === 0 ? (
            <p className="hint">No step ran yet.</p>
          ) : (
            <div className={styles.scroll}>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th scope="col">Step</th>
                    <th scope="col">Kind</th>
                    <th scope="col">Status</th>
                    <th scope="col">Tries</th>
                    <th scope="col">Error</th>
                  </tr>
                </thead>
                <tbody>
                  {instance.steps.map((step) => (
                    <tr key={step.step_id}>
                      <td>
                        {step.step_id}
                        {step.session_id ? (
                          <>
                            {" "}
                            <Link href={`/s/${step.session_id}`}>Open the session</Link>
                          </>
                        ) : null}
                      </td>
                      <td>{step.kind}</td>
                      <td>
                        <ToneBadge tone={instanceStatusTone(step.status)}>{instanceStatusLabel(step.status)}</ToneBadge>
                      </td>
                      <td>{step.attempts}</td>
                      <td>{step.error ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {actionError ? (
            <p className="alert alert-error" role="alert">
              {actionError}
            </p>
          ) : null}
          {canCancelInstance(instance.status) ? (
            <div className={styles.actions}>
              <button type="button" className="btn btn-small btn-danger" disabled={busy} onClick={cancel}>
                {busy ? "Cancelling…" : "Cancel the run"}
              </button>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
