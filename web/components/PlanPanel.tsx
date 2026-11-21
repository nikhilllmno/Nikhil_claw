"use client";

// Plan mode of a session (M7.9): the switch, the newest plan, and the decision. In plan mode the
// agent proposes a plan and does not change files. A person approves the plan, edits it and
// approves it, or rejects it. The parent owns the coding state and reloads it in `onChanged`.

import Link from "next/link";
import { useCallback, useId, useState } from "react";
import { errorText, type CodingOut, type PlanOut } from "@/lib/api";
import {
  MAX_PLAN_EDIT_CHARS,
  MAX_PLAN_FEEDBACK_CHARS,
  MAX_PLAN_TASK_CHARS,
  olderPlans,
  optionalText,
  planEditText,
  planStatusLabel,
  planTextChanged,
  planTone,
  proposedPlan,
} from "@/lib/coding";
import { toneBadge } from "@/lib/history";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import styles from "./Coding.module.css";
import { Markdown } from "./Markdown";
import page from "./Page.module.css";
import { useNow, usePolled } from "./usePolled";

export function PlanPanel({
  sessionId,
  coding,
  onChanged,
}: {
  sessionId: string;
  coding: CodingOut;
  /** Called after a change, so that the parent loads the coding state again. */
  onChanged: () => void;
}) {
  const api = useApi();
  const now = useNow();
  const newest = coding.plan;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const loadPlans = useCallback(() => api.listPlans(sessionId), [api, sessionId]);
  const plans = usePolled(loadPlans, "Could not load the older plans.");
  const older = olderPlans(plans.data ?? []).filter((p) => p.id !== newest?.id);

  /** Runs one call. It shows the error, or the note when the call works. */
  const act = async (call: () => Promise<string>) => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      setNote(await call());
    } catch (e) {
      setError(errorText(e, "The request failed."));
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  const toggle = () =>
    act(async () => {
      const { plan_mode: on } = await api.setPlanMode(sessionId, !coding.plan_mode);
      return on
        ? "Plan mode is on. The agent proposes a plan before it changes files."
        : "Plan mode is off. The agent can change files.";
    });

  return (
    <div className={styles.panel}>
      <div className={`card ${page.card}`}>
        <div className={page.row}>
          <label className={styles.check}>
            <input
              type="checkbox"
              role="switch"
              checked={coding.plan_mode}
              disabled={busy}
              onChange={() => void toggle()}
            />
            Plan mode is {coding.plan_mode ? "on" : "off"}
          </label>
        </div>
        <p className={styles.help}>
          In plan mode, the agent proposes a plan and does not change files. You approve the plan,
          edit it, or reject it. You can also type /plan TASK in the chat.
        </p>
      </div>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      {note ? (
        <p className="alert alert-success" role="status">
          {note} <Link href={`/s/${sessionId}`}>Open the session</Link>
        </p>
      ) : null}
      {newest === null ? (
        <p className="alert">The session has no plan yet. Describe a task, and the agent writes a plan.</p>
      ) : (
        <PlanCard key={newest.id} sessionId={sessionId} plan={newest} now={now} busy={busy} act={act} />
      )}
      {proposedPlan(newest) === null ? (
        <StartPlan
          sessionId={sessionId}
          disabled={busy}
          onStarted={(text) => {
            setNote(text);
            onChanged();
          }}
        />
      ) : null}
      {plans.error ? (
        <p className="alert alert-warning" role="status">
          {plans.error}{" "}
          <button type="button" className="btn btn-small" onClick={plans.reload}>
            Try again
          </button>
        </p>
      ) : null}
      {older.length > 0 ? (
        <section className={page.section} aria-label="Older versions">
          <h2 className={page.subheading}>Older versions</h2>
          <ul className={page.list}>
            {older.map((plan) => (
              <li key={plan.id}>
                <details>
                  <summary className={styles.summary}>
                    Plan {plan.version} · {planStatusLabel(plan.status)} · {relativeTime(plan.created_at, now)}
                  </summary>
                  <div className={styles.stack}>
                    <Markdown text={plan.body} />
                    {plan.feedback ? <p className={styles.feedback}>Feedback: {plan.feedback}</p> : null}
                  </div>
                </details>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

function PlanCard({
  sessionId,
  plan,
  now,
  busy,
  act,
}: {
  sessionId: string;
  plan: PlanOut;
  now: number;
  busy: boolean;
  act: (call: () => Promise<string>) => Promise<void>;
}) {
  const api = useApi();
  const ids = useId();
  const [mode, setMode] = useState<"view" | "edit" | "reject">("view");
  const [text, setText] = useState(() => planEditText(plan));
  const [feedback, setFeedback] = useState("");
  const waiting = plan.status === "proposed";

  const approve = (edited: boolean) =>
    act(async () => {
      const body = edited && planTextChanged(plan, text) ? text.trim() : undefined;
      await api.approvePlan(sessionId, plan.id, body);
      setMode("view");
      return body === undefined
        ? "Plan approved. The agent starts the work."
        : "Edited plan approved. The agent starts the work.";
    });

  const reject = () =>
    act(async () => {
      const answer = await api.rejectPlan(sessionId, plan.id, optionalText(feedback));
      setMode("view");
      setFeedback("");
      return answer.run_id === null
        ? "Plan rejected. Plan mode stays on."
        : "Plan rejected. The agent reads your feedback and writes a new plan.";
    });

  return (
    <section className={`card ${page.card}`} aria-label={`Plan ${plan.version}`}>
      <div className={page.cardHead}>
        <div className={page.name}>
          <span>Plan {plan.version}</span>
          <span className={toneBadge(planTone(plan.status))}>{planStatusLabel(plan.status)}</span>
          {plan.edited ? <span className="badge">Edited</span> : null}
        </div>
        <span className={page.meta}>{relativeTime(plan.created_at, now)}</span>
      </div>
      <div className={styles.plan}>
        <Markdown text={plan.body} />
      </div>
      {plan.feedback ? <p className={styles.feedback}>Feedback: {plan.feedback}</p> : null}
      {plan.edited ? (
        <details>
          <summary className={styles.summary}>The plan that the agent proposed</summary>
          <Markdown text={plan.proposed_body} />
        </details>
      ) : null}
      {waiting && mode === "view" ? (
        <div className={page.actions}>
          <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void approve(false)}>
            {busy ? "Working…" : "Approve"}
          </button>
          <button type="button" className="btn" disabled={busy} onClick={() => setMode("edit")}>
            Edit and approve
          </button>
          <button type="button" className="btn btn-danger" disabled={busy} onClick={() => setMode("reject")}>
            Reject
          </button>
        </div>
      ) : null}
      {waiting && mode === "edit" ? (
        <div className={page.form}>
          <div className="field">
            <label className="label" htmlFor={`${ids}-edit`}>
              Plan text (Markdown)
            </label>
            <textarea
              id={`${ids}-edit`}
              className={`input ${page.editor}`}
              value={text}
              maxLength={MAX_PLAN_EDIT_CHARS}
              onChange={(e) => setText(e.target.value)}
              disabled={busy}
            />
          </div>
          <div className={page.actions}>
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || text.trim() === ""}
              onClick={() => void approve(true)}
            >
              {busy ? "Working…" : "Approve the edited plan"}
            </button>
            <button type="button" className="btn" disabled={busy} onClick={() => setMode("view")}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      {waiting && mode === "reject" ? (
        <div className={page.form}>
          <div className="field">
            <label className="label" htmlFor={`${ids}-feedback`}>
              Feedback for the agent (optional)
            </label>
            <textarea
              id={`${ids}-feedback`}
              className="input"
              rows={3}
              value={feedback}
              maxLength={MAX_PLAN_FEEDBACK_CHARS}
              onChange={(e) => setFeedback(e.target.value)}
              placeholder="Say what is wrong. With feedback, the agent writes a new plan."
              disabled={busy}
            />
          </div>
          <div className={page.actions}>
            <button type="button" className="btn btn-danger" disabled={busy} onClick={() => void reject()}>
              {busy ? "Working…" : "Reject the plan"}
            </button>
            <button type="button" className="btn" disabled={busy} onClick={() => setMode("view")}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

/** A form that turns plan mode on and sends a task. */
function StartPlan({
  sessionId,
  disabled,
  onStarted,
}: {
  sessionId: string;
  disabled: boolean;
  onStarted: (note: string) => void;
}) {
  const api = useApi();
  const id = useId();
  const [task, setTask] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const text = task.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.startPlan(sessionId, text);
      setTask("");
      onStarted("The agent writes a plan. It shows here when it is ready.");
    } catch (e) {
      setError(errorText(e, "The task was not sent."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className={`card ${page.card}`} onSubmit={(e) => void submit(e)} aria-label="Ask for a plan">
      <div className="field">
        <label className="label" htmlFor={id}>
          Task for a new plan
        </label>
        <textarea
          id={id}
          className="input"
          rows={3}
          value={task}
          maxLength={MAX_PLAN_TASK_CHARS}
          onChange={(e) => setTask(e.target.value)}
          placeholder="Add a retry to the fetch call, and write a test."
          disabled={busy || disabled}
        />
      </div>
      {error ? (
        <p className={page.error} role="alert">
          {error}
        </p>
      ) : null}
      <div className={page.actions}>
        <button type="submit" className="btn" disabled={busy || disabled || task.trim() === ""}>
          {busy ? "Sending…" : "Ask for a plan"}
        </button>
      </div>
    </form>
  );
}
