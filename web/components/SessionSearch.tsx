"use client";

// The session search (docs/architecture.md, Section 8.6). The user finds the sessions that
// they can see by their words: their own sessions and the shared sessions of their
// workspaces.

import Link from "next/link";
import { useCallback, useState } from "react";
import { sessionTitle } from "@/lib/chat";
import { snippetParts } from "@/lib/history";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { PageHeader } from "./PageHeader";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

/** The most characters of a query (src/sama/search/core.py `MAX_QUERY_CHARS`). */
const MAX_QUERY_CHARS = 400;

/** A search that the user started. `from` is the workspace that the page showed at that time. */
type Asked = { q: string; workspace: string | null; org: string; from: string | null; run: number };

export function SessionSearch() {
  const { me, meError, reloadMe, workspaces, workspaceId, orgOfWorkspace } = useData();
  const org = orgOfWorkspace(workspaceId);

  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading" />;
  const workspace = workspaces.find((w) => w.id === workspaceId) ?? null;

  return (
    <div className={styles.page}>
      <PageHeader
        title="Search"
        meta={
          <>
            {org ? org.name : "No org"} · Find your sessions and the shared sessions of your
            workspaces by their words.
          </>
        }
      />
      {workspace === null || org === null ? (
        <p className="alert">You are not in a workspace yet. Ask an admin of your org to add you.</p>
      ) : (
        <SessionSearchForm orgId={org.id} workspaceId={workspace.id} workspaceLabel={workspace.name} />
      )}
    </div>
  );
}

/**
 * The search form and its results for one workspace. The search page and the Sessions section of
 * the console both use it. The results are for one search of one workspace: a new workspace
 * hides them.
 */
export function SessionSearchForm({
  orgId,
  workspaceId,
  workspaceLabel,
}: {
  orgId: string;
  workspaceId: string;
  workspaceLabel: string;
}) {
  const [text, setText] = useState("");
  const [onlyHere, setOnlyHere] = useState(true);
  const [asked, setAsked] = useState<Asked | null>(null);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const q = text.trim();
    if (q === "") return;
    setAsked((previous) => ({
      q,
      workspace: onlyHere ? workspaceId : null,
      org: orgId,
      from: workspaceId,
      run: (previous?.run ?? 0) + 1,
    }));
  };

  return (
    <>
      <form className={styles.form} role="search" onSubmit={submit}>
        <div className={styles.toolbar}>
          <label className={`field ${styles.grow}`}>
            <span className="label">Words to find</span>
            <input
              className="input"
              type="search"
              value={text}
              maxLength={MAX_QUERY_CHARS}
              placeholder="For example: deploy plan"
              onChange={(e) => setText(e.target.value)}
            />
          </label>
          <button type="submit" className="btn btn-primary" disabled={text.trim() === ""}>
            Search
          </button>
        </div>
        <label className={styles.name}>
          <input type="checkbox" checked={onlyHere} onChange={(e) => setOnlyHere(e.target.checked)} />
          <span>Only the workspace {workspaceLabel}</span>
        </label>
      </form>
      {asked !== null && asked.from === workspaceId ? (
        <SearchResults key={`${asked.org}|${asked.workspace ?? ""}|${asked.q}|${asked.run}`} asked={asked} />
      ) : null}
    </>
  );
}

function SearchResults({ asked }: { asked: Asked }) {
  const api = useApi();
  const load = useCallback(
    () => api.searchSessions(asked.org, asked.q, asked.workspace),
    [api, asked.org, asked.q, asked.workspace],
  );
  const result = useLoad(load, "The search failed.");
  const [now] = useState(() => Date.now());

  if (result.error) {
    return <ErrorState title="The search failed" message={result.error} onRetry={result.reload} />;
  }
  if (result.data === null) return <LoadingState label="Searching" />;
  const hits = result.data;
  if (hits.length === 0) {
    return (
      <p className="alert" role="status">
        No session has the words “{asked.q}”. Try other words.
      </p>
    );
  }
  return (
    <>
      <p className={styles.meta} role="status">
        {hits.length === 1 ? "1 session" : `${hits.length} sessions`} for “{asked.q}”
      </p>
      <ul className={styles.list} aria-label="Search results">
        {hits.map((hit) => (
          <li key={hit.session_id}>
            <section className={`card ${styles.card}`}>
              <div className={styles.cardHead}>
                <Link href={`/s/${hit.session_id}`} className={styles.heading}>
                  {sessionTitle({ id: hit.session_id, title: hit.title })}
                </Link>
                <div className={styles.name}>
                  {hit.audience === "shared" ? <span className="badge">Shared</span> : null}
                  <span className={styles.meta}>{relativeTime(hit.updated_at, now)}</span>
                </div>
              </div>
              {hit.snippets.length > 0 ? (
                <ul className={styles.entries}>
                  {hit.snippets.map((s) => (
                    <li key={s.message_id} className={styles.entry}>
                      <span className={styles.meta}>{s.role === "user" ? "You" : "Agent"}: </span>
                      {snippetParts(s.text).map((part, i) =>
                        part.match ? (
                          <mark key={i} className={styles.mark}>
                            {part.text}
                          </mark>
                        ) : (
                          part.text
                        ),
                      )}
                    </li>
                  ))}
                </ul>
              ) : null}
            </section>
          </li>
        ))}
      </ul>
    </>
  );
}
