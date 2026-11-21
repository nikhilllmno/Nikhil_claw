"use client";

// The screens of the Webhooks and Triggers sections (M7.10). A webhook is a trigger of the kind
// `webhook`: a signed address that starts a run or a workflow. A trigger is an event of a
// connected Composio app. Both have a list, a form, a detail with a test, and a delivery log.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  type Connection,
  type Trigger,
  type TriggerDelivery,
  type TriggerKind,
  type TriggerWithSecret,
  type Workflow,
  errorText,
} from "@/lib/api";
import { can } from "@/lib/console";
import { formatWhen } from "@/lib/jobs";
import { parseJsonObject, prettyJson } from "@/lib/jsonText";
import {
  NAME_MAX_CHARS,
  PAYLOAD_MAX_BYTES,
  PROMPT_MAX_CHARS,
  SAFE_TOOLSETS,
  SIGNATURE_HEADERS,
  SIGNATURE_KEY_HELP,
  SIGNED_TEXT,
  type TriggerDraft,
  buildNewTrigger,
  buildTriggerEdit,
  canManageTrigger,
  deliverySummary,
  isGoodOutcome,
  kindNoun,
  newTriggerDraft,
  outcomeLabel,
  outcomeTone,
  targetText,
  toTriggerDraft,
  triggerStatusLabel,
  triggerStatusTone,
  validateNewTrigger,
  validateTriggerEdit,
  workflowChoices,
} from "@/lib/triggers";
import { useApi } from "../AuthProvider";
import { ErrorState, LoadingState, ToneBadge } from "../ui";
import { useLoad } from "../useLoad";
import admin from "../Admin.module.css";
import styles from "../Page.module.css";
import { useConsole } from "./Console";
import { CopyBox } from "./kit";
import { SectionHeader, useAgentChoice } from "./parts";
import own from "./TriggerScreens.module.css";

/** The trigger of an answer that can have the secret. The secret stays out of the state. */
function withoutSecret(answer: TriggerWithSecret): Trigger {
  const { secret, ...trigger } = answer;
  void secret;
  return trigger;
}

/**
 * True while the component is on the screen. An answer that comes after the user left must not
 * put a secret back into the state, so a screen checks it after each request that gives one.
 */
function useAlive() {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return alive;
}

type View = { kind: "list" } | { kind: "new" } | { kind: "detail"; id: string };

/** The signing secret of a trigger, shown once after a create or a rotation. */
interface Revealed {
  triggerId: string;
  secret: string;
}

export function TriggerScreens({ kind }: { kind: TriggerKind }) {
  const api = useApi();
  const { org, workspaceId } = useConsole();
  const noun = kindNoun(kind);
  const sectionId = kind === "webhook" ? "webhooks" : "triggers";
  const [view, setView] = useState<View>({ kind: "list" });
  const [revealed, setRevealed] = useState<Revealed | null>(null);
  const mayCreate = can(org, "triggers.create");

  const loadTriggers = useCallback(
    async () => (await api.listTriggers(workspaceId)).filter((t) => t.kind === kind),
    [api, workspaceId, kind],
  );
  const { data: triggers, error, reload } = useLoad(loadTriggers, `Could not load the ${noun}s.`);
  // The workflows give the names of the targets and the choices of a new trigger. Without them,
  // the screens still work for the targets of the kind run.
  const loadWorkflows = useCallback(() => api.listWorkflows(org.id), [api, org.id]);
  const { data: workflows } = useLoad(loadWorkflows, "Could not load the workflows.");
  const loadConnections = useCallback(
    async () => (kind === "composio" ? await api.listConnections(workspaceId) : []),
    [api, workspaceId, kind],
  );
  const { data: connections } = useLoad(loadConnections, "Could not load the connections.");

  const back = () => {
    setRevealed(null);
    setView({ kind: "list" });
  };

  return (
    <>
      <SectionHeader
        id={sectionId}
        actions={
          view.kind === "list" && mayCreate ? (
            <button type="button" className="btn btn-primary btn-small" onClick={() => setView({ kind: "new" })}>
              New {noun}
            </button>
          ) : null
        }
        meta={mayCreate ? null : `Your role cannot create a ${noun}. Ask an org admin.`}
      />
      {view.kind === "new" ? (
        <TriggerForm
          kind={kind}
          trigger={null}
          workflows={workflows}
          connections={connections}
          onCancel={back}
          onSaved={(created) => {
            reload();
            if (created.secret) setRevealed({ triggerId: created.id, secret: created.secret });
            setView({ kind: "detail", id: created.id });
          }}
        />
      ) : view.kind === "detail" ? (
        <TriggerDetail
          key={view.id}
          kind={kind}
          triggerId={view.id}
          workflows={workflows}
          connections={connections}
          revealed={revealed?.triggerId === view.id ? revealed.secret : null}
          onReveal={(secret) => setRevealed({ triggerId: view.id, secret })}
          onDismissSecret={() => setRevealed(null)}
          onChanged={reload}
          onBack={back}
        />
      ) : error ? (
        <ErrorState title={`The ${noun}s did not load`} message={error} onRetry={reload} />
      ) : triggers === null ? (
        <LoadingState label={`Loading the ${noun}s`} />
      ) : triggers.length === 0 ? (
        <div className="state">
          <h2>{kind === "webhook" ? "No webhooks" : "No triggers"}</h2>
          <p>
            {kind === "webhook"
              ? "A webhook is a signed address. A system that you own calls it, and sama starts a run or a workflow."
              : "A trigger starts a run or a workflow when a connected app has an event, for example a new issue."}
          </p>
          {mayCreate ? (
            <button type="button" className="btn btn-primary" onClick={() => setView({ kind: "new" })}>
              New {noun}
            </button>
          ) : null}
        </div>
      ) : (
        <ul className={styles.list} aria-label={`${noun[0]?.toUpperCase()}${noun.slice(1)}s`}>
          {triggers.map((t) => (
            <TriggerRow
              key={t.id}
              trigger={t}
              workflows={workflows}
              onOpen={() => setView({ kind: "detail", id: t.id })}
            />
          ))}
        </ul>
      )}
    </>
  );
}

function useNames(trigger: Trigger, workflows: Workflow[] | null) {
  const { org } = useConsole();
  const agent = org.agent_profiles.find((p) => p.id === trigger.agent_profile_id)?.name ?? null;
  const workflow = workflows?.find((w) => w.id === trigger.workflow_id)?.name ?? null;
  return { agent, workflow };
}

function TriggerRow({
  trigger,
  workflows,
  onOpen,
}: {
  trigger: Trigger;
  workflows: Workflow[] | null;
  onOpen: () => void;
}) {
  const { agent, workflow } = useNames(trigger, workflows);
  return (
    <li className={`card ${styles.card}`}>
      <div className={styles.row}>
        <div className={styles.name}>
          {trigger.name}
          <ToneBadge tone={triggerStatusTone(trigger.status)}>{triggerStatusLabel(trigger.status)}</ToneBadge>
        </div>
        <div className={styles.actions}>
          <button type="button" className="btn btn-small" onClick={onOpen} aria-label={`Open ${trigger.name}`}>
            Open
          </button>
        </div>
      </div>
      <p className={styles.text}>
        {targetText(trigger, agent, workflow)}
        {trigger.kind === "composio" && trigger.composio_slug ? ` · ${trigger.composio_slug}` : ""}
      </p>
      {trigger.last_error ? (
        <p className={styles.error} role="note">
          Last error: {trigger.last_error}
        </p>
      ) : null}
    </li>
  );
}

// -- form -----------------------------------------------------------------------------------------

function TriggerForm({
  kind,
  trigger,
  workflows,
  connections,
  onCancel,
  onSaved,
}: {
  kind: TriggerKind;
  /** The trigger to edit, or `null` for a new one. */
  trigger: Trigger | null;
  workflows: Workflow[] | null;
  connections: Connection[] | null;
  onCancel: () => void;
  onSaved: (trigger: TriggerWithSecret) => void;
}) {
  const api = useApi();
  const { org, workspaceId, profiles, profileId } = useAgentChoice();
  const alive = useAlive();
  const noun = kindNoun(kind);
  const isNew = trigger === null;
  const [draft, setDraft] = useState<TriggerDraft>(() =>
    trigger ? toTriggerDraft(trigger) : newTriggerDraft(profileId ?? ""),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const edit = (change: Partial<TriggerDraft>) => setDraft((d) => ({ ...d, ...change }));

  // The tool groups that the agent of the trigger has, for the choices.
  const loadProfile = useCallback(
    () => (draft.agentId === "" ? Promise.resolve(null) : api.getAgentProfile(org.id, draft.agentId)),
    [api, org.id, draft.agentId],
  );
  const { data: profile } = useLoad(draft.target === "run" ? loadProfile : null, "Could not load the agent.");
  const toolsetNames = [
    ...new Set([...(profile?.options.toolsets.map((t) => t.name) ?? SAFE_TOOLSETS), ...draft.toolsets]),
  ];
  const flows = workflowChoices(workflows ?? [], workspaceId);
  const apps = (connections ?? []).filter((c) => c.kind === "composio");

  const toggle = (name: string) =>
    edit({
      toolsets: draft.toolsets.includes(name) ? draft.toolsets.filter((n) => n !== name) : [...draft.toolsets, name],
    });

  const problem = trigger ? validateTriggerEdit(draft, trigger) : validateNewTrigger(draft, kind);
  const changes = trigger ? buildTriggerEdit(trigger, draft) : null;
  const unchanged = changes !== null && Object.keys(changes).length === 0;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const saved = trigger
        ? await api.updateTrigger(workspaceId, trigger.id, changes ?? {})
        : await api.createTrigger(workspaceId, buildNewTrigger(draft, kind));
      if (alive.current) onSaved(saved);
    } catch (e) {
      setError(errorText(e, isNew ? `The ${noun} was not created.` : "The changes were not saved."));
    } finally {
      setBusy(false);
    }
  };

  const target = trigger ? trigger.target : draft.target;

  return (
    <form className={`card ${styles.card}`} onSubmit={submit} aria-labelledby="trigger-form">
      <div className={styles.cardHead}>
        <h2 id="trigger-form" className={styles.heading}>
          {isNew ? `New ${noun}` : `Edit ${trigger.name}`}
        </h2>
        <button type="button" className="btn btn-quiet btn-small" onClick={onCancel} disabled={busy}>
          {isNew ? "Back to the list" : "Cancel"}
        </button>
      </div>
      <label className="field">
        <span className="label">Name</span>
        <input
          className="input"
          value={draft.name}
          maxLength={NAME_MAX_CHARS}
          required
          disabled={busy}
          onChange={(e) => edit({ name: e.target.value })}
        />
      </label>

      {isNew ? (
        <fieldset className={admin.choices} disabled={busy}>
          <legend className="label">What it starts</legend>
          <label className={admin.choice}>
            <input type="radio" name="target" checked={draft.target === "run"} onChange={() => edit({ target: "run" })} />
            <span className={admin.choiceText}>
              <strong>A run of an agent</strong>
              <span className="hint">Each event starts a run with your prompt.</span>
            </span>
          </label>
          <label className={admin.choice}>
            <input
              type="radio"
              name="target"
              checked={draft.target === "workflow"}
              onChange={() => edit({ target: "workflow" })}
            />
            <span className={admin.choiceText}>
              <strong>A workflow</strong>
              <span className="hint">Each event starts a workflow that you own.</span>
            </span>
          </label>
        </fieldset>
      ) : null}

      {kind === "composio" && isNew ? (
        <>
          <label className="field">
            <span className="label">Connected app</span>
            <select
              className="input"
              value={draft.connectionId}
              disabled={busy}
              onChange={(e) => edit({ connectionId: e.target.value })}
            >
              <option value="">Choose an app</option>
              {apps.map((c) => (
                <option key={c.id} value={c.id} disabled={c.state !== "connected"}>
                  {c.connector}
                  {c.state === "connected" ? "" : ` (${c.state})`}
                </option>
              ))}
            </select>
            {connections !== null && apps.length === 0 ? (
              <span className="hint">
                This workspace has no app that you connected through Composio. <Link href="/connections">Connect an app</Link> first.
              </span>
            ) : null}
          </label>
          <label className="field">
            <span className="label">Trigger type</span>
            <input
              className="input"
              value={draft.slug}
              maxLength={100}
              placeholder="GITHUB_COMMIT_EVENT"
              disabled={busy}
              onChange={(e) => edit({ slug: e.target.value })}
            />
            <span className="hint">The name of the trigger type in Composio, for example GITHUB_COMMIT_EVENT.</span>
          </label>
          <label className="field">
            <span className="label">Config (optional)</span>
            <textarea
              className={`input ${styles.editor}`}
              rows={3}
              value={draft.config}
              disabled={busy}
              onChange={(e) => edit({ config: e.target.value })}
            />
            <span className="hint">A JSON object with the settings of the trigger type, for example the repository.</span>
          </label>
        </>
      ) : null}

      {target === "run" ? (
        <>
          {isNew ? (
            <label className="field">
              <span className="label">Agent</span>
              <select
                className="input"
                value={draft.agentId}
                disabled={busy}
                onChange={(e) => edit({ agentId: e.target.value })}
              >
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <label className="field">
            <span className="label">Prompt</span>
            <textarea
              className="input"
              rows={4}
              value={draft.prompt}
              maxLength={PROMPT_MAX_CHARS}
              disabled={busy}
              onChange={(e) => edit({ prompt: e.target.value })}
            />
            <span className="hint">
              What the agent does for each event. The run also gets the event as text.
            </span>
          </label>
          <fieldset className={admin.choices} disabled={busy}>
            <legend className="label">Tool groups</legend>
            {toolsetNames.map((name) => (
              <label key={name} className={admin.choice}>
                <input type="checkbox" checked={draft.toolsets.includes(name)} onChange={() => toggle(name)} />
                <span className={admin.choiceText}>
                  <strong>{name}</strong>
                  {SAFE_TOOLSETS.includes(name) ? null : <span className="hint">Only an admin can turn this on</span>}
                </span>
              </label>
            ))}
          </fieldset>
        </>
      ) : isNew ? (
        <label className="field">
          <span className="label">Workflow</span>
          <select
            className="input"
            value={draft.workflowId}
            disabled={busy}
            onChange={(e) => edit({ workflowId: e.target.value })}
          >
            <option value="">Choose a workflow</option>
            {flows.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
          <span className="hint">
            {workflows !== null && flows.length === 0
              ? "You have no workflow in this workspace. Make one in the section Workflows."
              : "Only a workflow that you own, in this workspace."}
          </span>
        </label>
      ) : (
        <p className="hint">The workflow of this {noun} cannot change. Make a new {noun} for another workflow.</p>
      )}

      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button type="submit" className="btn btn-primary" disabled={busy || unchanged}>
          {busy ? "Saving…" : isNew ? `Create the ${noun}` : "Save changes"}
        </button>
        <button type="button" className="btn" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

// -- detail ---------------------------------------------------------------------------------------

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className={own.fact}>
      <dt className={own.factLabel}>{label}</dt>
      <dd className={own.factValue}>{children}</dd>
    </div>
  );
}

function TriggerDetail({
  kind,
  triggerId,
  workflows,
  connections,
  revealed,
  onReveal,
  onDismissSecret,
  onChanged,
  onBack,
}: {
  kind: TriggerKind;
  triggerId: string;
  workflows: Workflow[] | null;
  connections: Connection[] | null;
  revealed: string | null;
  onReveal: (secret: string) => void;
  onDismissSecret: () => void;
  onChanged: () => void;
  onBack: () => void;
}) {
  const api = useApi();
  const { org, workspaceId } = useConsole();
  const alive = useAlive();
  const noun = kindNoun(kind);
  const load = useCallback(() => api.getTrigger(workspaceId, triggerId), [api, workspaceId, triggerId]);
  const { data: trigger, setData: setTrigger, error, reload } = useLoad(load, `Could not load the ${noun}.`);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [logVersion, setLogVersion] = useState(0);

  if (error) {
    return (
      <>
        <div className={styles.actions}>
          <button type="button" className="btn btn-quiet btn-small" onClick={onBack}>
            Back to the list
          </button>
        </div>
        <ErrorState title={`The ${noun} did not load`} message={error} onRetry={reload} />
      </>
    );
  }
  if (trigger === null) return <LoadingState label={`Loading the ${noun}`} />;

  const manage = canManageTrigger(org, trigger);
  const agent = org.agent_profiles.find((p) => p.id === trigger.agent_profile_id)?.name ?? null;
  const workflow = workflows?.find((w) => w.id === trigger.workflow_id)?.name ?? null;
  const connection = connections?.find((c) => c.id === trigger.connection_id) ?? null;

  const run = async (action: () => Promise<void>, failure: string) => {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      await action();
    } catch (e) {
      setActionError(errorText(e, failure));
    } finally {
      setBusy(false);
    }
  };

  const toggleStatus = () =>
    run(async () => {
      const next = trigger.status === "active" ? "disabled" : "active";
      setTrigger(await api.updateTrigger(workspaceId, trigger.id, { status: next }));
      onChanged();
      setNotice(next === "active" ? `The ${noun} is active.` : `The ${noun} is disabled. It takes no event now.`);
    }, "The status was not changed.");

  const rotate = async () => {
    if (!window.confirm("Rotate the secret? The old secret stops working at once. Every sender must use the new secret.")) {
      return;
    }
    await run(async () => {
      const rotated = await api.rotateTriggerSecret(workspaceId, trigger.id);
      // The user can have left this screen. Then the new secret is not shown, and the user rotates again.
      if (!alive.current) return;
      setTrigger(withoutSecret(rotated));
      if (rotated.secret) onReveal(rotated.secret);
      onChanged();
    }, "The secret was not rotated.");
  };

  const remove = async () => {
    if (!window.confirm(`Delete the ${noun}?`)) return;
    await run(async () => {
      await api.deleteTrigger(workspaceId, trigger.id);
      onChanged();
      onBack();
    }, `The ${noun} was not deleted.`);
  };

  return (
    <>
      <div className={styles.actions}>
        <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={onBack}>
          Back to the list
        </button>
      </div>

      {revealed ? (
        <section className="alert alert-warning" role="alert" aria-label="New secret">
          <div className={styles.form}>
            <p>
              <strong>Copy the secret now.</strong> Sama shows it only this one time. Store it in a safe place. If you
              lose it, rotate the secret.
            </p>
            <CopyBox value={revealed} label="Signing secret" />
            <div className={styles.actions}>
              <button type="button" className="btn btn-small" onClick={onDismissSecret}>
                I copied the secret
              </button>
            </div>
          </div>
        </section>
      ) : null}

      {editing ? (
        <TriggerForm
          kind={kind}
          trigger={trigger}
          workflows={workflows}
          connections={connections}
          onCancel={() => setEditing(false)}
          onSaved={(saved) => {
            setTrigger(withoutSecret(saved));
            onChanged();
            setEditing(false);
            setNotice("Saved.");
          }}
        />
      ) : (
        <section className={`card ${styles.card}`} aria-labelledby="trigger-overview">
          <div className={styles.cardHead}>
            <h2 id="trigger-overview" className={styles.heading}>
              {trigger.name}
            </h2>
            <ToneBadge tone={triggerStatusTone(trigger.status)}>{triggerStatusLabel(trigger.status)}</ToneBadge>
          </div>
          <dl className={own.facts}>
            <Fact label="Starts">{targetText(trigger, agent, workflow)}</Fact>
            {trigger.target === "run" ? (
              <>
                <Fact label="Prompt">
                  <pre className={styles.pre}>{trigger.prompt}</pre>
                </Fact>
                <Fact label="Tool groups">
                  {trigger.toolsets.length === 0 ? (
                    "None"
                  ) : (
                    <span className={admin.chips}>
                      {trigger.toolsets.map((name) => (
                        <span key={name} className={admin.chip}>
                          {name}
                        </span>
                      ))}
                    </span>
                  )}
                </Fact>
              </>
            ) : null}
            {kind === "composio" ? (
              <>
                <Fact label="App">{connection ? `${connection.connector} (${connection.state})` : (trigger.connection_id ?? "None")}</Fact>
                <Fact label="Trigger type">{trigger.composio_slug ?? "None"}</Fact>
                {trigger.composio_config && Object.keys(trigger.composio_config).length > 0 ? (
                  <Fact label="Config">
                    <pre className={styles.pre}>{prettyJson(trigger.composio_config)}</pre>
                  </Fact>
                ) : null}
              </>
            ) : null}
            <Fact label="Created">{formatWhen(trigger.created_at)}</Fact>
            <Fact label="Changed">{formatWhen(trigger.updated_at)}</Fact>
          </dl>
          {trigger.last_error ? (
            <p className="alert alert-error" role="note">
              Last error: {trigger.last_error}
            </p>
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
          {manage ? (
            <div className={styles.actions}>
              <button type="button" className="btn btn-small" disabled={busy} onClick={() => setEditing(true)}>
                Edit
              </button>
              <button type="button" className="btn btn-small" disabled={busy} onClick={toggleStatus}>
                {trigger.status === "active" ? "Disable" : "Enable"}
              </button>
              {kind === "webhook" ? (
                <button type="button" className="btn btn-small" disabled={busy} onClick={rotate}>
                  Rotate the secret
                </button>
              ) : null}
              <button type="button" className="btn btn-small btn-danger" disabled={busy} onClick={remove}>
                Delete
              </button>
            </div>
          ) : (
            <p className="hint">Only the owner of the {noun} or an org admin can change it.</p>
          )}
        </section>
      )}

      {kind === "webhook" ? <WebhookAddress url={trigger.url ?? null} /> : null}

      <TestCard
        kind={kind}
        trigger={trigger}
        manage={manage}
        onSent={() => setLogVersion((n) => n + 1)}
      />
      <DeliveryLog workspaceId={workspaceId} triggerId={trigger.id} version={logVersion} />
    </>
  );
}

function WebhookAddress({ url }: { url: string | null }) {
  return (
    <section className={`card ${styles.card}`} aria-labelledby="webhook-address">
      <h2 id="webhook-address" className={styles.heading}>
        Address and signature
      </h2>
      {url ? (
        <div className="field">
          <span className="label">Address</span>
          <CopyBox value={url} label="Webhook address" />
          <span className="hint">The sender makes a POST request to this address with a JSON body.</span>
        </div>
      ) : (
        <p className="alert" role="note">
          Sama has no public address, so it cannot show the address of the webhook. An admin sets the public address
          of sama.
        </p>
      )}
      <p className={styles.text}>
        Each request must have the headers of the Standard Webhooks scheme. The secret of the webhook signs the request.
      </p>
      <ul className={own.headers}>
        {SIGNATURE_HEADERS.map((h) => (
          <li key={h.name}>
            <span className={own.headerName}>{h.name}</span>
            <p className={styles.text}>{h.text}</p>
          </li>
        ))}
      </ul>
      <p className={styles.text}>The signed text is:</p>
      <pre className={styles.pre}>{SIGNED_TEXT}</pre>
      <p className="hint">{SIGNATURE_KEY_HELP}</p>
    </section>
  );
}

function TestCard({
  kind,
  trigger,
  manage,
  onSent,
}: {
  kind: TriggerKind;
  trigger: Trigger;
  manage: boolean;
  onSent: () => void;
}) {
  const api = useApi();
  const { workspaceId } = useConsole();
  const [text, setText] = useState("");
  const [start, setStart] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<TriggerDelivery | null>(null);
  const what = trigger.target === "workflow" ? "workflow" : "run";

  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    const parsed = parseJsonObject(text, "The payload", PAYLOAD_MAX_BYTES);
    if (!parsed.ok) {
      setError(parsed.error);
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const answer = await api.testTrigger(workspaceId, trigger.id, { payload: parsed.value, start });
      setResult(answer.delivery);
      onSent();
    } catch (e) {
      setError(errorText(e, "The test was not sent."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className={`card ${styles.card}`} onSubmit={send} aria-labelledby="trigger-test">
      <h2 id="trigger-test" className={styles.heading}>
        Send a test
      </h2>
      <p className="hint">
        {kind === "webhook"
          ? "Sama signs a test delivery with the secret of the webhook and checks it like a real delivery."
          : "Sama checks a test delivery like a real event. A test has no signature."}{" "}
        A test starts no {what} unless you choose it.
      </p>
      <label className="field">
        <span className="label">Payload (optional)</span>
        <textarea
          className={`input ${styles.editor}`}
          rows={4}
          value={text}
          disabled={busy || !manage}
          spellCheck={false}
          onChange={(e) => setText(e.target.value)}
        />
        <span className="hint">A JSON object of at most 20,000 bytes. Empty means a small test payload.</span>
      </label>
      <label className={own.checkRow}>
        <input type="checkbox" checked={start} disabled={busy || !manage} onChange={(e) => setStart(e.target.checked)} />
        Also start a {what}
      </label>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      {result ? (
        <div className={isGoodOutcome(result.outcome) ? "alert alert-success" : "alert alert-error"} role="status">
          {deliverySummary(result)}{" "}
          {result.session_id ? <Link href={`/s/${result.session_id}`}>Open the session</Link> : null}
        </div>
      ) : null}
      {manage ? (
        <div className={styles.actions}>
          <button type="submit" className="btn btn-primary btn-small" disabled={busy}>
            {busy ? "Sending…" : "Send the test"}
          </button>
        </div>
      ) : (
        <p className="hint">Only the owner of the {kindNoun(kind)} or an org admin can send a test.</p>
      )}
    </form>
  );
}

function DeliveryLog({
  workspaceId,
  triggerId,
  version,
}: {
  workspaceId: string;
  triggerId: string;
  /** A new number means that a delivery came (a test), so the log loads again. */
  version: number;
}) {
  const api = useApi();
  const load = useCallback(() => api.triggerDeliveries(workspaceId, triggerId), [api, workspaceId, triggerId]);
  const { data: rows, error, reload } = useLoad(load, "Could not load the deliveries.");
  useEffect(() => {
    if (version > 0) reload();
  }, [version, reload]);

  return (
    <section className={`card ${styles.card}`} aria-labelledby="trigger-log">
      <div className={styles.cardHead}>
        <h2 id="trigger-log" className={styles.heading}>
          Delivery log
        </h2>
        <button type="button" className="btn btn-small" onClick={reload}>
          Refresh
        </button>
      </div>
      <p className="hint">The last deliveries, newest first. Sama keeps no payload.</p>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : rows === null ? (
        <LoadingState label="Loading the deliveries" />
      ) : rows.length === 0 ? (
        <p className="hint">No delivery yet.</p>
      ) : (
        <div className={styles.scroll}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Result</th>
                <th scope="col">HTTP</th>
                <th scope="col">Error</th>
                <th scope="col">Started</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>{formatWhen(row.received_at)}</td>
                  <td>
                    <span className={admin.chips}>
                      <ToneBadge tone={outcomeTone(row.outcome)}>{outcomeLabel(row.outcome)}</ToneBadge>
                      {row.test ? <ToneBadge tone="neutral">Test</ToneBadge> : null}
                    </span>
                  </td>
                  <td>{row.http_status}</td>
                  <td>{row.error ?? ""}</td>
                  <td>
                    {row.session_id ? (
                      <Link href={`/s/${row.session_id}`}>Open the session</Link>
                    ) : row.workflow_instance_id ? (
                      "A workflow instance"
                    ) : (
                      ""
                    )}
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
