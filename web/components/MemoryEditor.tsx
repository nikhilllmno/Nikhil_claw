"use client";

// The memory editor (docs/architecture.md, Section 8.4). The user reads and changes the notes
// that the agents keep: the agent notes, the user profile, and the workspace notes. Each save
// is a new revision, and an old revision can come back.

import { useCallback, useRef, useState } from "react";
import { errorText, type MemoryNotes, type MemoryTarget, type MemoryWritten } from "@/lib/api";
import { cleanEntries, entriesProblem, memoryUsed, TARGET_HELP, TARGET_TITLES } from "@/lib/memory";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { PageHeader } from "./PageHeader";
import { ErrorState, LoadingState } from "./ui";
import { useFocusWhen } from "./useFocusWhen";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

/** The most characters of a reason (src/sama/memory/core.py `MAX_REASON_CHARS`). */
const MAX_REASON_CHARS = 200;

export function MemoryEditor() {
  const { me, meError, reloadMe, workspaces, workspaceId, profilesFor } = useData();
  const profiles = profilesFor(workspaceId);
  const [chosen, setChosen] = useState<string | null>(null);
  const profileId = chosen !== null && profiles.some((p) => p.id === chosen) ? chosen : (profiles[0]?.id ?? null);

  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the memory" />;
  const workspace = workspaces.find((w) => w.id === workspaceId) ?? null;

  return (
    <div className={styles.page}>
      <PageHeader
        title="Memory"
        meta={
          <>
            {workspace ? workspace.label : "No workspace"} · The notes that the agents keep between
            sessions. Each save makes a new revision.
          </>
        }
      />
      {workspace === null ? (
        <p className="alert">You are not in a workspace yet. Ask an admin of your org to add you.</p>
      ) : (
        <>
          {profiles.length > 0 ? (
            <label className="field">
              <span className="label">Agent</span>
              <select className="input" value={profileId ?? ""} onChange={(e) => setChosen(e.target.value)}>
                {profiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <MemoryTargets key={`${workspace.id}:${profileId ?? ""}`} workspaceId={workspace.id} profileId={profileId} />
        </>
      )}
    </div>
  );
}

function MemoryTargets({ workspaceId, profileId }: { workspaceId: string; profileId: string | null }) {
  const api = useApi();
  const load = useCallback(() => api.getMemory(workspaceId, profileId), [api, workspaceId, profileId]);
  const memory = useLoad(load, "Could not load the memory.");
  if (memory.error) {
    return <ErrorState title="The memory did not load" message={memory.error} onRetry={memory.reload} />;
  }
  if (memory.data === null) return <LoadingState label="Loading the memory" />;
  return (
    <ul className={styles.list}>
      {memory.data.targets.map((notes) => (
        <li key={notes.target}>
          <NotesCard
            workspaceId={workspaceId}
            profileId={profileId}
            notes={notes}
            onSaved={(saved) =>
              memory.setData((current) =>
                current === null
                  ? current
                  : {
                      ...current,
                      targets: current.targets.map((t) => (t.target === saved.target ? { ...t, ...saved } : t)),
                    },
              )
            }
          />
        </li>
      ))}
    </ul>
  );
}

function NotesCard({
  workspaceId,
  profileId,
  notes,
  onSaved,
}: {
  workspaceId: string;
  profileId: string | null;
  notes: MemoryNotes;
  onSaved: (saved: MemoryWritten) => void;
}) {
  const api = useApi();
  const [draft, setDraft] = useState<string[] | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [history, setHistory] = useState(false);
  const firstEntry = useRef<HTMLTextAreaElement>(null);
  const editButton = useRef<HTMLButtonElement>(null);
  useFocusWhen(draft !== null, firstEntry, editButton);
  const agentId = notes.target === "agent" ? profileId : null;
  const title = TARGET_TITLES[notes.target];

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (draft === null) return;
    const entries = cleanEntries(draft);
    const problem = entriesProblem(entries, notes.cap);
    if (problem !== null) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const saved = await api.editMemory(workspaceId, notes.target, entries, agentId, reason.trim() || null);
      onSaved(saved);
      setDraft(null);
      setReason("");
      setNotice(`Saved as revision ${saved.revision}.`);
    } catch (e) {
      setError(errorText(e, "The notes were not saved."));
    } finally {
      setBusy(false);
    }
  };

  const used = draft === null ? notes.chars : memoryUsed(cleanEntries(draft));
  const over = used > notes.cap;

  return (
    <section className={`card ${styles.card}`} aria-label={title}>
      <div className={styles.cardHead}>
        <h2 className={styles.heading}>{title}</h2>
        <span className={over ? styles.error : styles.meta} aria-live="polite">
          {used} of {notes.cap} characters
        </span>
      </div>
      <p className={styles.text}>{TARGET_HELP[notes.target]}</p>
      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}

      {draft === null ? (
        notes.entries.length === 0 ? (
          <p className={styles.meta}>No notes yet.</p>
        ) : (
          <ul className={styles.entries}>
            {notes.entries.map((entry, i) => (
              <li key={i} className={styles.entry}>
                {entry}
              </li>
            ))}
          </ul>
        )
      ) : (
        <form className={styles.form} onSubmit={save} aria-label={`Edit ${title}`}>
          {draft.map((entry, i) => (
            <div key={i} className={styles.form}>
              <label className="field">
                <span className="label">Entry {i + 1}</span>
                <textarea
                  ref={i === 0 ? firstEntry : undefined}
                  className="input"
                  rows={3}
                  value={entry}
                  onChange={(e) => setDraft((d) => d && d.map((v, j) => (j === i ? e.target.value : v)))}
                />
              </label>
              <div className={styles.actions}>
                <button
                  type="button"
                  className="btn btn-quiet btn-small"
                  onClick={() => setDraft((d) => d && d.filter((_, j) => j !== i))}
                >
                  Remove entry {i + 1}
                </button>
              </div>
            </div>
          ))}
          <div className={styles.actions}>
            <button type="button" className="btn btn-small" onClick={() => setDraft((d) => d && [...d, ""])}>
              Add entry
            </button>
          </div>
          <label className="field">
            <span className="label">Reason (optional)</span>
            <input className="input" value={reason} maxLength={MAX_REASON_CHARS} onChange={(e) => setReason(e.target.value)} />
            <span className="hint">The history shows the reason.</span>
          </label>
          {error ? (
            <p className="alert alert-error" role="alert">
              {error}
            </p>
          ) : null}
          <div className={styles.actions}>
            <button type="submit" className="btn btn-primary btn-small" disabled={busy}>
              {busy ? "Saving" : "Save"}
            </button>
            <button
              type="button"
              className="btn btn-quiet btn-small"
              disabled={busy}
              onClick={() => {
                setDraft(null);
                setError(null);
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      <div className={styles.actions}>
        {draft === null && notes.can_edit ? (
          <button
            ref={editButton}
            type="button"
            className="btn btn-small"
            onClick={() => {
              setNotice(null);
              setDraft(notes.entries.length > 0 ? [...notes.entries] : [""]);
            }}
          >
            Edit
          </button>
        ) : null}
        <button
          type="button"
          className="btn btn-quiet btn-small"
          aria-expanded={history}
          onClick={() => setHistory((h) => !h)}
        >
          {history ? "Hide history" : "History"}
        </button>
      </div>
      {!notes.can_edit ? (
        <p className={styles.meta}>Only a workspace admin can change these notes.</p>
      ) : null}
      {history ? (
        <History
          key={notes.revision}
          workspaceId={workspaceId}
          target={notes.target}
          agentId={agentId}
          revision={notes.revision}
          canEdit={notes.can_edit}
          onReverted={(saved) => {
            onSaved(saved);
            setDraft(null);
            setNotice(`Reverted. The notes are now revision ${saved.revision}.`);
          }}
        />
      ) : null}
    </section>
  );
}

function History({
  workspaceId,
  target,
  agentId,
  revision,
  canEdit,
  onReverted,
}: {
  workspaceId: string;
  target: MemoryTarget;
  agentId: string | null;
  revision: number;
  canEdit: boolean;
  onReverted: (saved: MemoryWritten) => void;
}) {
  const api = useApi();
  const load = useCallback(
    () => api.listMemoryRevisions(workspaceId, target, agentId),
    [api, workspaceId, target, agentId],
  );
  const revisions = useLoad(load, "Could not load the history.");
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [now] = useState(() => Date.now());

  const revert = async (seq: number) => {
    if (!window.confirm(`Set the notes back to revision ${seq}? This makes a new revision.`)) return;
    setBusy(seq);
    setError(null);
    try {
      onReverted(await api.revertMemory(workspaceId, target, seq, agentId, null));
    } catch (e) {
      setError(errorText(e, "The notes were not reverted."));
    } finally {
      setBusy(null);
    }
  };

  if (revisions.error) {
    return <ErrorState title="The history did not load" message={revisions.error} onRetry={revisions.reload} />;
  }
  if (revisions.data === null) return <LoadingState label="Loading the history" />;
  return (
    <div className={styles.section}>
      <h3 className={styles.subheading}>History</h3>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      {revisions.data.length === 0 ? (
        <p className={styles.meta}>No revisions yet.</p>
      ) : (
        <ul className={styles.list}>
          {revisions.data.map((r) => (
            <li key={r.seq} className={styles.form}>
              <div className={styles.row}>
                <div className={styles.name}>
                  <span>Revision {r.seq}</span>
                  <span className="badge">{r.author_kind === "user" ? "Person" : "Agent"}</span>
                  <span className={styles.meta}>
                    {r.action}
                    {r.reverted_to !== null ? ` to ${r.reverted_to}` : ""} · {relativeTime(r.created_at, now)}
                  </span>
                </div>
                <div className={styles.actions}>
                  <button
                    type="button"
                    className="btn btn-quiet btn-small"
                    aria-expanded={open === r.seq}
                    onClick={() => setOpen((o) => (o === r.seq ? null : r.seq))}
                  >
                    {open === r.seq ? "Hide" : "Show"}
                  </button>
                  {canEdit && r.seq !== revision ? (
                    <button
                      type="button"
                      className="btn btn-small"
                      disabled={busy !== null}
                      onClick={() => void revert(r.seq)}
                    >
                      {busy === r.seq ? "Reverting" : "Revert to this"}
                    </button>
                  ) : null}
                </div>
              </div>
              {r.reason ? <p className={styles.text}>Reason: {r.reason}</p> : null}
              {open === r.seq ? (
                r.entries.length === 0 ? (
                  <p className={styles.meta}>No notes in this revision.</p>
                ) : (
                  <ul className={styles.entries}>
                    {r.entries.map((entry, i) => (
                      <li key={i} className={styles.entry}>
                        {entry}
                      </li>
                    ))}
                  </ul>
                )
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
