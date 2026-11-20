"use client";

import { useState } from "react";
import { useApi } from "./AuthProvider";
import type { Approval } from "@/lib/api";
import styles from "./Cards.module.css";

type Action = "approve" | "always" | "deny";

export function ApprovalCard({ approval, onDecided }: { approval: Approval; onDecided: () => void }) {
  const api = useApi();
  const [busy, setBusy] = useState<Action | null>(null);
  const [error, setError] = useState<string | null>(null);

  const decide = async (action: Action) => {
    setBusy(action);
    setError(null);
    try {
      if (action === "deny") await api.deny(approval.id);
      else await api.approve(approval.id, action === "always");
      onDecided();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The decision was not saved.");
      setBusy(null);
    }
  };

  const args = JSON.stringify(approval.arguments, null, 2);
  return (
    <section className={`card ${styles.card}`} aria-label={`Approval for ${approval.tool_name}`}>
      <h2 className={styles.title}>
        The agent wants to use <span className="mono">{approval.tool_name}</span>
      </h2>
      {approval.reason ? <p className={styles.reason}>{approval.reason}</p> : null}
      {args !== "{}" ? <pre className={styles.code}>{args}</pre> : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button
          type="button"
          className="btn btn-primary"
          disabled={busy !== null}
          onClick={() => decide("approve")}
        >
          {busy === "approve" ? "Approving…" : "Approve"}
        </button>
        <button type="button" className="btn" disabled={busy !== null} onClick={() => decide("always")}>
          {busy === "always" ? "Saving…" : "Always allow"}
        </button>
        <button
          type="button"
          className="btn btn-danger"
          disabled={busy !== null}
          onClick={() => decide("deny")}
        >
          {busy === "deny" ? "Denying…" : "Deny"}
        </button>
      </div>
      <p className="hint">
        Always allow lets this agent use {approval.tool_name} for you without asking again.
      </p>
    </section>
  );
}
