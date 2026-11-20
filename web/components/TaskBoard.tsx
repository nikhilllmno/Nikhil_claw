"use client";

// The task board of a workspace (docs/architecture.md, Section 12.4): one column for each
// status, a form for a new card, and the details of one card with its comments.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  type AgentProfileSummary,
  type TaskComment,
  type TaskDetail,
  type TaskOut,
  type TaskPatch,
  type TaskStatus,
  errorText,
} from "@/lib/api";
import {
  COLUMNS,
  EMPTY_DRAFT,
  MAX_BODY,
  MAX_COMMENT,
  MAX_TITLE,
  type NewTaskDraft,
  STATUS_LABEL,
  assigneeLabel,
  assigneePatch,
  assigneeValue,
  columnsOf,
  hasLiveCards,
  newTaskBody,
  statusChoices,
  upsertTask,
  validateNewTask,
} from "@/lib/tasks";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./TaskBoard.module.css";

/** The board loads again after this time while an agent works on a card. */
const LIVE_RELOAD_MS = 10_000;

export function TaskBoard() {
  const api = useApi();
  const { me, meError, reloadMe, workspaces, workspaceId, profilesFor } = useData();
  const load = useMemo(
    () => (workspaceId === null ? null : () => api.listTasks(workspaceId)),
    [api, workspaceId],
  );
  const { data, setData, error, reload } = useLoad(load, "Could not load the task board.");
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const profiles = profilesFor(workspaceId);
  const tasks = useMemo(() => data?.tasks ?? [], [data]);
  const userId = data?.user_id ?? null;
  const live = hasLiveCards(tasks);

  // A card that an agent works on changes without the user, so load the board again.
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => {
      reload();
      setNow(Date.now());
    }, LIVE_RELOAD_MS);
    return () => clearInterval(timer);
  }, [live, reload]);

  // A new workspace closes the open card and the form.
  const [lastWorkspace, setLastWorkspace] = useState(workspaceId);
  if (workspaceId !== lastWorkspace) {
    setLastWorkspace(workspaceId);
    setSelected(null);
    setCreating(false);
  }

  const changed = useCallback(
    (task: TaskOut) => setData((current) => (current ? { ...current, tasks: upsertTask(current.tasks, task) } : current)),
    [setData],
  );

  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the task board" />;
  if (workspaceId === null) {
    return (
      <ErrorState
        title="No workspace"
        message="You are not in a workspace yet. Ask an admin of your org to add you."
      />
    );
  }
  const workspace = workspaces.find((w) => w.id === workspaceId);
  const columns = columnsOf(tasks);

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1 className={styles.title}>Task board</h1>
          <p className={styles.meta}>
            {workspace?.label}. Give a card to an agent and it starts when its dependencies are done.
          </p>
        </div>
        <div className={styles.actions}>
          <button type="button" className="btn btn-small" onClick={reload}>
            Refresh
          </button>
          <button
            type="button"
            className="btn btn-primary btn-small"
            aria-expanded={creating}
            aria-controls="new-card"
            onClick={() => setCreating((open) => !open)}
          >
            New card
          </button>
        </div>
      </header>

      {creating ? (
        <NewCardForm
          workspaceId={workspaceId}
          profiles={profiles}
          userId={userId}
          onCreated={(task) => {
            changed(task);
            setCreating(false);
            setSelected(task.id);
          }}
          onCancel={() => setCreating(false)}
        />
      ) : null}

      {selected !== null ? (
        <CardDetail
          key={selected}
          workspaceId={workspaceId}
          taskId={selected}
          profiles={profiles}
          userId={userId}
          onChanged={changed}
          onClose={() => setSelected(null)}
        />
      ) : null}

      {error && data === null ? (
        <ErrorState title="The task board did not load" message={error} onRetry={reload} />
      ) : data === null ? (
        <LoadingState label="Loading the task board" />
      ) : (
        <>
          {error ? (
            <p className="alert alert-error" role="alert">
              {error}
            </p>
          ) : null}
          <div className={styles.columns}>
            {COLUMNS.map((column) => {
              const cards = columns.get(column.status) ?? [];
              return (
                <section key={column.status} className={styles.column} aria-labelledby={`col-${column.status}`}>
                  <h2 id={`col-${column.status}`} className={styles.columnTitle}>
                    {column.title} <span className="badge">{cards.length}</span>
                  </h2>
                  {cards.length === 0 ? (
                    <p className={styles.empty}>{column.empty}</p>
                  ) : (
                    <ul className={styles.cards}>
                      {cards.map((task) => (
                        <li key={task.id}>
                          <button
                            type="button"
                            className={styles.card}
                            aria-pressed={task.id === selected}
                            onClick={() => setSelected(task.id)}
                          >
                            <span className={styles.cardTitle}>{task.title}</span>
                            <span className={styles.cardMeta}>
                              <span>{assigneeLabel(task, profiles, userId)}</span>
                              {task.audience === "private" ? <span className="badge">Private</span> : null}
                              {task.depends_on.length > 0 ? (
                                <span className="badge">
                                  {task.depends_on.length === 1
                                    ? "1 dependency"
                                    : `${task.depends_on.length} dependencies`}
                                </span>
                              ) : null}
                              <span>{relativeTime(task.created_at, now)}</span>
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

function AssigneeOptions({
  profiles,
  includeOther,
}: {
  profiles: readonly AgentProfileSummary[];
  includeOther: boolean;
}) {
  return (
    <>
      <option value="none">No one</option>
      <option value="me">Me</option>
      {includeOther ? (
        <option value="person" disabled>
          Another person
        </option>
      ) : null}
      {profiles.map((p) => (
        <option key={p.id} value={`agent:${p.id}`}>
          Agent: {p.name}
        </option>
      ))}
    </>
  );
}

function NewCardForm({
  workspaceId,
  profiles,
  userId,
  onCreated,
  onCancel,
}: {
  workspaceId: string;
  profiles: readonly AgentProfileSummary[];
  userId: string | null;
  onCreated: (task: TaskOut) => void;
  onCancel: () => void;
}) {
  const api = useApi();
  const [draft, setDraft] = useState<NewTaskDraft>(EMPTY_DRAFT);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleRef = useRef<HTMLInputElement>(null);
  useEffect(() => titleRef.current?.focus(), []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const problem = validateNewTask(draft);
    if (problem !== null) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onCreated(await api.createTask(workspaceId, newTaskBody(draft, userId)));
    } catch (e) {
      setError(errorText(e, "Could not make the card."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form id="new-card" className={`card ${styles.panel}`} onSubmit={submit} aria-label="New card">
      <h2 className={styles.panelTitle}>New card</h2>
      <label className="field">
        <span className="label">Title</span>
        <input
          ref={titleRef}
          className="input"
          value={draft.title}
          maxLength={MAX_TITLE}
          onChange={(e) => setDraft({ ...draft, title: e.target.value })}
        />
      </label>
      <label className="field">
        <span className="label">Details</span>
        <textarea
          className="input"
          rows={4}
          value={draft.body}
          maxLength={MAX_BODY}
          onChange={(e) => setDraft({ ...draft, body: e.target.value })}
        />
        <span className="hint">An agent reads only the title and the details. Put all it needs here.</span>
      </label>
      <div className={styles.row}>
        <label className="field">
          <span className="label">Assignee</span>
          <select
            className="input"
            value={draft.assignee}
            onChange={(e) => setDraft({ ...draft, assignee: e.target.value })}
          >
            <AssigneeOptions profiles={profiles} includeOther={false} />
          </select>
        </label>
        <label className="field">
          <span className="label">Who can see it</span>
          <select
            className="input"
            value={draft.audience}
            onChange={(e) => setDraft({ ...draft, audience: e.target.value === "private" ? "private" : "shared" })}
          >
            <option value="shared">The workspace</option>
            <option value="private">Only me</option>
          </select>
        </label>
      </div>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Saving" : "Make the card"}
        </button>
        <button type="button" className="btn btn-quiet" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function CardDetail({
  workspaceId,
  taskId,
  profiles,
  userId,
  onChanged,
  onClose,
}: {
  workspaceId: string;
  taskId: string;
  profiles: readonly AgentProfileSummary[];
  userId: string | null;
  onChanged: (task: TaskOut) => void;
  onClose: () => void;
}) {
  const api = useApi();
  const load = useCallback(() => api.getTask(workspaceId, taskId), [api, workspaceId, taskId]);
  const { data, setData, error, reload } = useLoad<TaskDetail>(load, "Could not load the card.");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [now] = useState(() => Date.now());
  const headingRef = useRef<HTMLHeadingElement>(null);
  const loaded = data !== null;
  useEffect(() => {
    if (loaded) headingRef.current?.focus();
  }, [loaded]);

  const apply = async (patch: TaskPatch | null) => {
    if (patch === null || data === null) return;
    setBusy(true);
    setActionError(null);
    try {
      const task = await api.updateTask(workspaceId, taskId, patch);
      setData({ ...data, task });
      onChanged(task);
    } catch (e) {
      setActionError(errorText(e, "Could not change the card."));
    } finally {
      setBusy(false);
    }
  };

  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    const body = comment.trim();
    if (body === "" || data === null) return;
    setBusy(true);
    setActionError(null);
    try {
      const added: TaskComment = await api.addTaskComment(workspaceId, taskId, body);
      setData({ ...data, comments: [...data.comments, added] });
      setComment("");
    } catch (e) {
      setActionError(errorText(e, "Could not add the comment."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={`card ${styles.panel}`} aria-label="Card details" aria-busy={data === null}>
      {error && data === null ? (
        <ErrorState title="The card did not load" message={error} onRetry={reload} />
      ) : data === null ? (
        <LoadingState label="Loading the card" />
      ) : (
        <>
          <div className={styles.row}>
            <h2 ref={headingRef} tabIndex={-1} className={styles.panelTitle}>
              {data.task.title}
            </h2>
            <button type="button" className="btn btn-quiet btn-small" onClick={onClose}>
              Close
            </button>
          </div>
          {data.task.body ? <p className={styles.text}>{data.task.body}</p> : null}
          {data.task.result ? (
            <div className="field">
              <span className="label">Result</span>
              <p className={styles.text}>{data.task.result}</p>
            </div>
          ) : null}
          <div className={styles.row}>
            <label className="field">
              <span className="label">Status</span>
              <select
                className="input"
                value={data.task.status}
                disabled={busy}
                onChange={(e) => {
                  const status = e.target.value as TaskStatus;
                  if (status !== "running") void apply({ status });
                }}
              >
                {statusChoices(data.task).map((s) => (
                  <option key={s} value={s} disabled={s === "running"}>
                    {STATUS_LABEL[s]}
                  </option>
                ))}
              </select>
              {data.task.status === "running" ? (
                <span className="hint">A new status stops the agent run of this card.</span>
              ) : null}
            </label>
            <label className="field">
              <span className="label">Assignee</span>
              <select
                className="input"
                value={assigneeValue(data.task, userId)}
                disabled={busy || data.task.status === "running"}
                onChange={(e) => void apply(assigneePatch(e.target.value, userId))}
              >
                <AssigneeOptions
                  profiles={profiles}
                  includeOther={assigneeValue(data.task, userId) === "person"}
                />
              </select>
            </label>
          </div>
          <p className={styles.meta}>
            {data.task.audience === "private" ? "Only you can see this card. " : ""}
            {data.task.session_id ? (
              <Link href={`/s/${data.task.session_id}`}>Open the agent session</Link>
            ) : null}
          </p>
          {actionError ? (
            <p className="alert alert-error" role="alert">
              {actionError}
            </p>
          ) : null}

          <h3 className={styles.subTitle}>Comments</h3>
          {data.comments.length === 0 ? (
            <p className={styles.empty}>No comments yet.</p>
          ) : (
            <ul className={styles.comments}>
              {data.comments.map((c) => (
                <li key={c.id} className={styles.comment}>
                  <span className={styles.cardMeta}>
                    {c.author_kind === "user"
                      ? c.author_user_id === userId
                        ? "You"
                        : "A person"
                      : c.author_kind === "agent"
                        ? "Agent"
                        : "Task board"}
                    {" · "}
                    {relativeTime(c.created_at, now)}
                  </span>
                  <p className={styles.text}>{c.body}</p>
                </li>
              ))}
            </ul>
          )}
          <form className={styles.commentForm} onSubmit={send}>
            <label className="field">
              <span className="label">Add a comment</span>
              <textarea
                className="input"
                rows={2}
                value={comment}
                maxLength={MAX_COMMENT}
                onChange={(e) => setComment(e.target.value)}
              />
            </label>
            <button type="submit" className="btn btn-small" disabled={busy || comment.trim() === ""}>
              Send
            </button>
          </form>
        </>
      )}
    </section>
  );
}
