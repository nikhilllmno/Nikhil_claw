"use client";

import { useMemo, useState } from "react";
import { type AgentProfile, type ProfileChange, errorText } from "@/lib/api";
import { canRevert, changeTitle } from "@/lib/profileHistory";
import { useApi } from "./AuthProvider";
import { useLoad } from "./useLoad";
import cards from "./Cards.module.css";
import styles from "./AgentSettings.module.css";

/**
 * The change history of an agent profile, newest first (M6.1). An org owner or admin can revert
 * a change. `version` reloads the history when the profile changes.
 */
export function ProfileHistory({
  orgId,
  profile,
  version,
  onReverted,
}: {
  orgId: string;
  profile: AgentProfile;
  version: string;
  onReverted: (profile: AgentProfile) => void;
}) {
  const api = useApi();
  const load = useMemo(
    // `version` is in the list so that a saved edit or a revert loads the history again.
    () => (version ? () => api.listAgentProfileChanges(orgId, profile.id) : null),
    [api, orgId, profile.id, version],
  );
  const { data: changes, error, reload } = useLoad(load, "Could not load the history.");
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [revertError, setRevertError] = useState<string | null>(null);

  const revert = async (change: ProfileChange) => {
    setBusy(change.id);
    setRevertError(null);
    try {
      const result = await api.revertAgentProfileChange(orgId, profile.id, change.id);
      setConfirming(null);
      onReverted(result.profile);
    } catch (e) {
      setRevertError(errorText(e, "The change was not reverted."));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className={`card ${styles.section}`} aria-labelledby="history-heading">
      <h2 className={styles.heading} id="history-heading">
        History
      </h2>
      <p className="hint">
        Changes to the soul, the settings, the tools, and the policy rules of this agent. A revert
        writes the old values again. It applies to new sessions.
      </p>
      {revertError ? (
        <p className="alert alert-error" role="alert">
          {revertError}
        </p>
      ) : null}
      {error ? (
        <div className="alert alert-error" role="alert">
          <span>{error}</span>{" "}
          <button type="button" className="btn btn-small" onClick={reload}>
            Retry
          </button>
        </div>
      ) : changes === null ? (
        <p className="hint" aria-live="polite">
          Loading the history…
        </p>
      ) : changes.length === 0 ? (
        <p className="hint">No changes yet.</p>
      ) : (
        <ul className={styles.subagents}>
          {changes.map((change) => (
            <li key={change.id} className={styles.subagent}>
              <div className={styles.row}>
                <h3 className={styles.subTitle}>{changeTitle(change)}</h3>
                <time className="hint" dateTime={change.created_at}>
                  {new Date(change.created_at).toLocaleString()}
                </time>
              </div>
              {change.summary ? <p>{change.summary}</p> : null}
              {change.diff ? <pre className={cards.code}>{change.diff}</pre> : null}
              {change.reverted_by_change_id !== null ? (
                <p className="hint">Reverted.</p>
              ) : canRevert(change, profile.editable) ? (
                <div className={cards.actions}>
                  {confirming === change.id ? (
                    <>
                      <button
                        type="button"
                        className="btn btn-small btn-danger"
                        disabled={busy !== null}
                        onClick={() => revert(change)}
                      >
                        {busy === change.id ? "Reverting…" : "Confirm revert"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-small"
                        disabled={busy !== null}
                        onClick={() => setConfirming(null)}
                      >
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      className="btn btn-small"
                      disabled={busy !== null}
                      onClick={() => {
                        setRevertError(null);
                        setConfirming(change.id);
                      }}
                    >
                      Revert
                    </button>
                  )}
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
