"use client";

// The session search (docs/architecture.md, Section 8.6). The user finds the sessions that
// they can see by their words: their own sessions and the shared sessions of their
// workspaces.

import Link from "next/link";
import { useEffect, useState } from "react";
import { errorText, type SearchHit } from "@/lib/api";
import { sessionTitle } from "@/lib/chat";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { PageHeader } from "./PageHeader";
import { ErrorState, LoadingState } from "./ui";
import styles from "./Page.module.css";

/** The most characters of a query (src/sama/search/core.py `MAX_QUERY_CHARS`). */
const MAX_QUERY_CHARS = 400;
/** The most sessions of one search (src/sama/search/core.py `MAX_LIMIT`). */
const MAX_RESULTS = 10;

type Result = { key: string; hits: SearchHit[] };

export function SessionSearch() {
  const api = useApi();
  const { me, meError, reloadMe, workspaces, workspaceId, orgOfWorkspace } = useData();
  const [text, setText] = useState("");
  const [onlyHere, setOnlyHere] = useState(true);
  const [asked, setAsked] = useState<{ q: string; workspace: string | null; org: string } | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const org = orgOfWorkspace(workspaceId);
  const key = asked === null ? null : `${asked.org}|${asked.workspace ?? ""}|${asked.q}|${tick}`;

  useEffect(() => {
    if (asked === null || key === null) return;
    let live = true;
    api.searchSessions(asked.org, asked.q, asked.workspace, MAX_RESULTS).then(
      (hits) => {
        if (!live) return;
        setResult({ key, hits });
        setError(null);
        setNow(Date.now());
      },
      (e: unknown) => {
        if (live) setError(errorText(e, "The search failed."));
      },
    );
    return () => {
      live = false;
    };
  }, [api, asked, key]);

  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading" />;
  const workspace = workspaces.find((w) => w.id === workspaceId) ?? null;

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const q = text.trim();
    if (q === "" || org === null) return;
    setError(null);
    setAsked({ q, workspace: onlyHere ? workspaceId : null, org: org.id });
    setTick((n) => n + 1);
  };

  const hits = result !== null && result.key === key ? result.hits : null;

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
            <span>Only the workspace {workspace.name}</span>
          </label>
        </form>
      )}
      {asked === null ? null : error ? (
        <ErrorState title="The search failed" message={error} onRetry={() => setTick((n) => n + 1)} />
      ) : hits === null ? (
        <LoadingState label="Searching" />
      ) : hits.length === 0 ? (
        <p className="alert" role="status">
          No session has the words “{asked.q}”. Try other words.
        </p>
      ) : (
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
                          {s.text}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </section>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
