"use client";

// The checkpoints of the session files, with Undo and Restore (M7.9). A checkpoint is a copy of
// the files before a tool call. Restore asks for a confirmation first.

import { useCallback, useState } from "react";
import { errorText } from "@/lib/api";
import { checkpointStamp } from "@/lib/chat";
import {
  CHECKPOINTS_CHANGED,
  checkpointWhat,
  restoreTarget,
  type RestoreTarget,
  targetStillValid,
  undoTarget,
} from "@/lib/coding";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import styles from "./Coding.module.css";
import page from "./Page.module.css";
import { ErrorState, LoadingState } from "./ui";
import { useNow, usePolled } from "./usePolled";

export function Checkpoints({
  sessionId,
  onChanged,
}: {
  sessionId: string;
  /** Called after an undo or a restore. */
  onChanged?: () => void;
}) {
  const api = useApi();
  const now = useNow();
  const load = useCallback(() => api.listCheckpoints(sessionId), [api, sessionId]);
  const list = usePolled(load, "Could not load the checkpoints.");
  /** What waits for a confirmation. A restore holds the commit of the checkpoint, not only its number. */
  const [confirming, setConfirming] = useState<RestoreTarget | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [answer, setAnswer] = useState<string | null>(null);

  // A new poll can shift the numbers. When the checkpoint that the user chose is gone or has
  // another number, the confirm step ends and the user looks at the list again.
  if (confirming !== null && list.data !== null && !targetStillValid(confirming, list.data)) {
    setConfirming(null);
    setError(CHECKPOINTS_CHANGED);
  }

  const restore = async (target: RestoreTarget) => {
    setBusy(true);
    setError(null);
    setAnswer(null);
    try {
      const done =
        target.kind === "undo"
          ? await api.undo(sessionId)
          : await api.rollback(sessionId, target.number, target.commit);
      setAnswer(done.text);
      setConfirming(null);
      list.reload();
      onChanged?.();
    } catch (e) {
      setError(errorText(e, "The files were not restored."));
      // The list may be the cause (409). Read it again, and end the confirm step.
      setConfirming(null);
      list.reload();
    } finally {
      setBusy(false);
    }
  };

  if (list.data === null) {
    if (list.error) return <ErrorState title="The checkpoints did not load" message={list.error} onRetry={list.reload} />;
    return <LoadingState label="Loading the checkpoints" />;
  }

  const confirmBox = (target: RestoreTarget, text: string) => (
    <span className={styles.confirm} role="group" aria-label="Confirm">
      <span>{text}</span>
      <button type="button" className="btn btn-small btn-danger" disabled={busy} onClick={() => void restore(target)}>
        {busy ? "Restoring…" : "Yes, restore"}
      </button>
      <button type="button" className="btn btn-small" disabled={busy} onClick={() => setConfirming(null)}>
        Cancel
      </button>
    </span>
  );

  return (
    <div className={styles.panel}>
      <p className={styles.help}>
        A checkpoint is a copy of the session files before a tool call. Undo restores the files to
        the state before the last run. Restore puts the files back to one checkpoint. sama saves
        the current files as a new checkpoint first.
      </p>
      {list.error ? (
        <p className="alert alert-warning" role="status">
          {list.error}
        </p>
      ) : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      {answer ? (
        <p className="alert alert-success" role="status">
          {answer}
        </p>
      ) : null}
      {list.data.length === 0 ? (
        <p className="alert">The session has no checkpoints yet.</p>
      ) : (
        <>
          <div className={page.actions}>
            {confirming?.kind === "undo" ? (
              confirmBox(confirming, "Undo the file changes of the last run?")
            ) : (
              <button type="button" className="btn" disabled={busy} onClick={() => setConfirming(undoTarget(list.data ?? []))}>
                Undo
              </button>
            )}
          </div>
          <ol className={page.list} aria-label="Checkpoints, newest first">
            {list.data.map((item) => (
              <li key={item.number}>
                <section className={`card ${page.card}`} aria-label={`Checkpoint ${item.number}`}>
                  <div className={page.cardHead}>
                    <div className={page.name}>
                      <span className="badge">{item.number}</span>
                      <span>{checkpointWhat(item)}</span>
                    </div>
                    <span className={page.meta} title={checkpointStamp(item.created_at)}>
                      {checkpointStamp(item.created_at)} · {relativeTime(item.created_at, now)}
                    </span>
                  </div>
                  <div className={page.actions}>
                    {confirming?.kind === "restore" && confirming.number === item.number ? (
                      confirmBox(confirming, `Restore the files to checkpoint ${item.number}?`)
                    ) : (
                      <button
                        type="button"
                        className="btn btn-small"
                        disabled={busy}
                        aria-label={`Restore checkpoint ${item.number}`}
                        onClick={() => setConfirming(restoreTarget(item))}
                      >
                        Restore
                      </button>
                    )}
                  </div>
                </section>
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}
