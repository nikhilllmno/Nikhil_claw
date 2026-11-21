"use client";

// The Sessions section: the sessions of the workspace in an Active and an Archived list, a search
// of the words in them, and the actions on one session: open, rename, archive or restore, and
// export, and the direct mode of a coding agent (M7.2). The retention of the org is at the end.
// The owner of a session renames or archives it. For a shared session an admin does it too.

import Link from "next/link";
import { useCallback, useRef, useState } from "react";
import { errorText, type ExportFormat, type SessionOut } from "@/lib/api";
import { saveFile } from "@/lib/admin";
import { sessionTitle } from "@/lib/chat";
import {
  archiveConfirmText,
  canManageSession,
  DIRECT_CHOICES,
  type DirectChoice,
  directAgent,
  directBadge,
  directChoice,
  hasDirectMode,
  checkTitle,
  emptyText,
  LIST_LIMIT,
  maybeTruncated,
  replaceSession,
  SESSION_FILTERS,
  type SessionFilter,
  sessionsOfWorkspace,
  titleChanged,
  withoutSession,
} from "@/lib/sessionList";
import { relativeTime } from "@/lib/time";
import { useApi } from "../../AuthProvider";
import { Retention } from "../../DataControls";
import { useData } from "../../DataProvider";
import { SessionSearchForm } from "../../SessionSearch";
import { ErrorState, LoadingState } from "../../ui";
import { useFocusWhen } from "../../useFocusWhen";
import { useLoad } from "../../useLoad";
import { useConsole } from "../Console";
import { SectionHeader } from "../parts";
import styles from "../../Page.module.css";
import own from "../../Admin.module.css";

export function SessionsSection() {
  const { org, workspaceId } = useConsole();
  const workspace = org.workspaces.find((w) => w.id === workspaceId);
  return (
    <>
      <SectionHeader
        id="sessions"
        meta={`${workspace?.name ?? "No workspace"} · You change your own sessions. An admin also changes the shared sessions.`}
      />
      <SessionSearchBox />
      <SessionList workspaceName={workspace?.name ?? "This workspace"} />
      <Retention org={org} />
    </>
  );
}

function SessionSearchBox() {
  const { org, workspaceId } = useConsole();
  const workspace = org.workspaces.find((w) => w.id === workspaceId);
  return (
    <section className={styles.card} aria-labelledby="sessions-search">
      <h2 id="sessions-search" className={styles.heading}>
        Search
      </h2>
      <SessionSearchForm orgId={org.id} workspaceId={workspaceId} workspaceLabel={workspace?.name ?? "this workspace"} />
    </section>
  );
}

function SessionList({ workspaceName }: { workspaceName: string }) {
  const api = useApi();
  const { workspaceId } = useConsole();
  const { reloadSessions } = useData();
  const [filter, setFilter] = useState<SessionFilter>("active");
  const load = useCallback(() => api.listSessions(LIST_LIMIT, filter), [api, filter]);
  const sessions = useLoad(load, "Could not load the sessions.");
  const [notice, setNotice] = useState<string | null>(null);
  const [now] = useState(() => Date.now());

  const rows = sessions.data === null ? null : sessionsOfWorkspace(sessions.data, workspaceId);
  const changeFilter = (next: SessionFilter) => {
    if (next === filter) return;
    setFilter(next);
    setNotice(null);
    // The answer of the other list replaces the rows, so the old rows go at once.
    sessions.setData(null);
  };

  return (
    <section className={`card ${styles.card}`} aria-labelledby="sessions-list">
      <div className={styles.cardHead}>
        <h2 id="sessions-list" className={styles.heading}>
          Sessions
        </h2>
        <div className={own.segmented} role="group" aria-label="Show sessions">
          {SESSION_FILTERS.map((f) => (
            <button key={f.id} type="button" aria-pressed={filter === f.id} onClick={() => changeFilter(f.id)}>
              {f.label}
            </button>
          ))}
        </div>
      </div>
      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      {sessions.error ? (
        <ErrorState title="The sessions did not load" message={sessions.error} onRetry={sessions.reload} />
      ) : rows === null ? (
        <LoadingState label="Loading the sessions" />
      ) : rows.length === 0 ? (
        <p className="alert">{emptyText(filter, workspaceName)}</p>
      ) : (
        <ul className={styles.list} aria-label={filter === "active" ? "Active sessions" : "Archived sessions"}>
          {rows.map((session) => (
            <SessionRow
              key={session.id}
              session={session}
              now={now}
              onChanged={(changed, text) => {
                // A change of the status moves the session to the other list.
                sessions.setData((all) =>
                  all === null
                    ? all
                    : changed.status === filter
                      ? replaceSession(all, changed)
                      : withoutSession(all, changed.id),
                );
                if (text !== null) setNotice(text);
                reloadSessions();
              }}
            />
          ))}
        </ul>
      )}
      {sessions.data !== null && maybeTruncated(sessions.data.length) ? (
        <p className={styles.meta}>
          The list shows the newest {LIST_LIMIT} sessions. Use the search to find older ones.
        </p>
      ) : null}
    </section>
  );
}

function SessionRow({
  session,
  now,
  onChanged,
}: {
  session: SessionOut;
  now: number;
  onChanged: (session: SessionOut, notice: string | null) => void;
}) {
  const api = useApi();
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const titleInput = useRef<HTMLInputElement>(null);
  const renameButton = useRef<HTMLButtonElement>(null);
  // The focus goes to the field when the form opens, and back to the button when it closes.
  useFocusWhen(renaming, titleInput, renameButton);
  const title = sessionTitle(session);
  const archived = session.status === "archived";
  const manage = canManageSession(session);

  const run = async (label: string, action: () => Promise<void>, fallback: string) => {
    setBusy(label);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(errorText(e, fallback));
    } finally {
      setBusy(null);
    }
  };

  const rename = (event: React.FormEvent) => {
    event.preventDefault();
    const checked = checkTitle(draft);
    if ("error" in checked) {
      setError(checked.error);
      return;
    }
    void run(
      "rename",
      async () => {
        const changed = await api.updateSession(session.id, { title: checked.title });
        setRenaming(false);
        onChanged(changed, `The session is renamed to ${checked.title}.`);
      },
      "The session was not renamed.",
    );
  };

  const setStatus = (status: "active" | "archived") => {
    if (status === "archived" && !window.confirm(archiveConfirmText(title))) return;
    void run(
      status,
      async () => {
        const changed = await api.updateSession(session.id, { status });
        onChanged(changed, status === "archived" ? `The session ${title} is archived.` : `The session ${title} is restored.`);
      },
      status === "archived" ? "The session was not archived." : "The session was not restored.",
    );
  };

  const switchDirect = (choice: DirectChoice) =>
    run(
      "direct",
      async () => {
        const agent = directAgent(choice);
        const answer = await api.codingAgent(session.id, agent);
        onChanged({ ...session, coding_agent: agent }, answer.text);
      },
      "The messages of the session still go to the same place.",
    );

  const download = (format: ExportFormat) =>
    run(
      format,
      async () => {
        const file = await api.exportSession(session.id, format);
        saveFile(file.name, file.blob);
      },
      "The export failed.",
    );

  return (
    <li className={styles.section}>
      <div className={styles.row}>
        <div className={styles.name}>
          <Link href={`/s/${session.id}`}>{title}</Link>
          <span className={session.audience === "shared" ? "badge badge-accent" : "badge"}>
            {session.audience === "shared" ? "Shared" : "Private"}
          </span>
          {archived ? <span className="badge badge-warning">Archived</span> : null}
          {directBadge(session) ? <span className="badge badge-accent">{directBadge(session)}</span> : null}
          <span className={styles.meta}>Made {relativeTime(session.created_at, now)}</span>
        </div>
      </div>
      {renaming ? (
        <form className={styles.toolbar} onSubmit={rename} aria-label={`Rename ${title}`}>
          <label className={`field ${styles.grow}`}>
            <span className="label">New title</span>
            <input
              ref={titleInput}
              className="input"
              value={draft}
              maxLength={200}
              onChange={(e) => setDraft(e.target.value)}
              disabled={busy !== null}
            />
          </label>
          <button type="submit" className="btn btn-primary btn-small" disabled={busy !== null || !titleChanged(session.title, draft)}>
            {busy === "rename" ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            className="btn btn-quiet btn-small"
            disabled={busy !== null}
            onClick={() => {
              setRenaming(false);
              setError(null);
            }}
          >
            Cancel
          </button>
        </form>
      ) : (
        <div className={styles.actions}>
          <Link href={`/s/${session.id}`} className="btn btn-small">
            Open
          </Link>
          {manage ? (
            <>
            <button
              ref={renameButton}
              type="button"
              className="btn btn-small"
              disabled={busy !== null}
              onClick={() => {
                setDraft(session.title ?? "");
                setError(null);
                setRenaming(true);
              }}
            >
              Rename
            </button>
            {archived ? (
              <button type="button" className="btn btn-small" disabled={busy !== null} onClick={() => setStatus("active")}>
                {busy === "active" ? "Restoring…" : "Restore"}
              </button>
            ) : (
              <button type="button" className="btn btn-small" disabled={busy !== null} onClick={() => setStatus("archived")}>
                {busy === "archived" ? "Archiving…" : "Archive"}
              </button>
            )}
            </>
          ) : null}
          <button type="button" className="btn btn-quiet btn-small" disabled={busy !== null} onClick={() => void download("markdown")}>
            {busy === "markdown" ? "Preparing…" : "Export as Markdown"}
          </button>
          <button type="button" className="btn btn-quiet btn-small" disabled={busy !== null} onClick={() => void download("json")}>
            {busy === "json" ? "Preparing…" : "Export as JSON"}
          </button>
        </div>
      )}
      {hasDirectMode(session) && !renaming ? (
        <label className="field">
          <span className="label">Messages go to</span>
          <select
            className="input"
            value={directChoice(session)}
            disabled={busy !== null}
            onChange={(e) => void switchDirect(e.target.value as DirectChoice)}
          >
            {DIRECT_CHOICES.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
          <span className="hint">
            {busy === "direct"
              ? "Saving…"
              : "The direct mode sends each message to Claude Code or Codex, like /claude and /codex in a chat."}
          </span>
        </label>
      ) : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </li>
  );
}
