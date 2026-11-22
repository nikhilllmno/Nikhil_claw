"use client";

import { useEffect, useRef, useState } from "react";
import { useApi } from "./AuthProvider";
import { type Approval, errorText } from "@/lib/api";
import { approvalKey, type ApprovalAction } from "@/lib/approvalKeys";
import { toolLabel, toolSubject } from "@/lib/toolView";
import styles from "./Cards.module.css";

const DONE_TEXT: Record<ApprovalAction, string> = {
  approve: "Allowed. The run goes on.",
  always: "Always allowed. The run goes on.",
  deny: "Denied. The agent gets the answer.",
};

/**
 * A tool call that waits for a person, as Claude Code asks: what the agent wants to do in one
 * line, the full input on demand, and the answers with keys (1 allow, 2 always, 3 deny). The
 * keys work for the `active` card when the focus is not in a text field, or the message box
 * is empty.
 */
export function ApprovalCard({
  approval,
  onDecided,
  active = false,
}: {
  approval: Approval;
  onDecided: () => void;
  /** The first card that waits: the keys answer it. */
  active?: boolean;
}) {
  const api = useApi();
  const [busy, setBusy] = useState<ApprovalAction | null>(null);
  const [error, setError] = useState<string | null>(null);

  // A proposal of a change of the agent shows its diff. A rule cannot approve a proposal, so it
  // has no "Always allow".
  const proposal = approval.proposal === true;

  const decide = async (action: ApprovalAction) => {
    if (busy !== null) return;
    setBusy(action);
    setError(null);
    try {
      if (action === "deny") await api.deny(approval.id);
      else await api.approve(approval.id, action === "always");
      onDecided();
    } catch (e) {
      setError(errorText(e, "The decision was not saved."));
      setBusy(null);
    }
  };
  const decideRef = useRef(decide);
  useEffect(() => {
    decideRef.current = decide;
  });

  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      const action = approvalKey(event, document.activeElement, !proposal);
      if (action === null) return;
      event.preventDefault();
      void decideRef.current(action);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, proposal]);

  const args = JSON.stringify(approval.arguments, null, 2);
  const subject = toolSubject(approval.tool_name, approval.arguments);
  const label = toolLabel(approval.tool_name);

  if (busy !== null && error === null) {
    return (
      <section className={`card ${styles.card} ${styles.decided}`} aria-label={`Approval for ${approval.tool_name}`}>
        <p className={styles.decidedText} role="status">
          <span className="spinner" aria-hidden="true" /> {DONE_TEXT[busy]}
        </p>
      </section>
    );
  }

  return (
    <section className={`card ${styles.card}`} aria-label={`Approval for ${approval.tool_name}`}>
      <div className={styles.ask}>
        <h2 className={styles.title}>
          {proposal ? "The agent proposes a change" : "Allow this step?"}
        </h2>
        <p className={styles.what}>
          <span className={styles.verb}>{label}</span>
          {subject ? <span className={styles.subject}>{subject}</span> : null}
          <span className={styles.tool}>{approval.tool_name}</span>
        </p>
      </div>
      {approval.reason ? <p className={styles.reason}>{approval.reason}</p> : null}
      {approval.diff ? (
        <pre className={styles.code} aria-label="The change">
          {approval.diff}
        </pre>
      ) : args !== "{}" ? (
        <details className={styles.more}>
          <summary className={styles.moreSummary}>Show the full input</summary>
          <pre className={styles.code}>{args}</pre>
        </details>
      ) : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button
          type="button"
          className="btn btn-primary"
          aria-keyshortcuts={active ? "1" : undefined}
          onClick={() => void decide("approve")}
        >
          {proposal ? "Approve" : "Allow once"}
          {active ? <kbd className={styles.key}>1</kbd> : null}
        </button>
        {!proposal ? (
          <button
            type="button"
            className="btn"
            aria-keyshortcuts={active ? "2" : undefined}
            onClick={() => void decide("always")}
            title={`Let this agent use ${approval.tool_name} for you without asking again.`}
          >
            Always allow
            {active ? <kbd className={styles.key}>2</kbd> : null}
          </button>
        ) : null}
        <button
          type="button"
          className="btn btn-danger"
          aria-keyshortcuts={active ? "3" : undefined}
          onClick={() => void decide("deny")}
        >
          Deny
          {active ? <kbd className={styles.key}>3</kbd> : null}
        </button>
      </div>
      {proposal && approval.diff ? <p className="hint">After the approval, the change applies to new sessions.</p> : null}
    </section>
  );
}
