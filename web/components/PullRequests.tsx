"use client";

// The pull requests of the session, and the form that asks the agent to open one (M7.9). The
// agent commits, pushes a `sama/*` branch, and calls `pr_create`. A person approves that call
// on the approval card in the chat.

import Link from "next/link";
import { useCallback, useId, useState } from "react";
import { errorText, type PullRequestOut } from "@/lib/api";
import { optionalText, pullRequestStatusLabel, pullRequestTitle, pullRequestTone, safeUrl } from "@/lib/coding";
import { toneBadge } from "@/lib/history";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import styles from "./Coding.module.css";
import page from "./Page.module.css";
import { ErrorState, LoadingState } from "./ui";
import { useNow, usePolled } from "./usePolled";

export function PullRequests({ sessionId, onRequested }: { sessionId: string; onRequested?: () => void }) {
  const api = useApi();
  const ids = useId();
  const now = useNow();
  const load = useCallback(() => api.listPullRequests(sessionId), [api, sessionId]);
  const list = usePolled(load, "Could not load the pull requests.");
  const [repo, setRepo] = useState("");
  const [title, setTitle] = useState("");
  const [base, setBase] = useState("");
  const [draft, setDraft] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      await api.requestPullRequest(sessionId, {
        repo: optionalText(repo),
        title: optionalText(title),
        base: optionalText(base),
        draft,
      });
      setNote(
        "The agent commits the changes, pushes a branch, and opens the pull request. A person must approve the pr_create call on the approval card in the chat.",
      );
      setTitle("");
      list.reload();
      onRequested?.();
    } catch (e) {
      setError(errorText(e, "The request was not sent."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.panel}>
      <form className={`card ${page.card}`} onSubmit={(e) => void submit(e)} aria-label="Open a pull request">
        <h2 className={page.subheading}>Open a pull request</h2>
        <p className={styles.help}>
          The agent does the work. A person approves the pr_create call on the approval card in the chat.
          Leave a field empty to let the agent choose.
        </p>
        <div className={styles.fields}>
          <div className="field">
            <label className="label" htmlFor={`${ids}-repo`}>
              Repo (optional)
            </label>
            <input
              id={`${ids}-repo`}
              className="input"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="owner/name"
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
            />
          </div>
          <div className="field">
            <label className="label" htmlFor={`${ids}-title`}>
              Title (optional)
            </label>
            <input
              id={`${ids}-title`}
              className="input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              disabled={busy}
            />
          </div>
          <div className="field">
            <label className="label" htmlFor={`${ids}-base`}>
              Base branch (optional)
            </label>
            <input
              id={`${ids}-base`}
              className="input"
              value={base}
              onChange={(e) => setBase(e.target.value)}
              placeholder="main"
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
            />
          </div>
        </div>
        <label className={styles.check}>
          <input type="checkbox" checked={draft} onChange={(e) => setDraft(e.target.checked)} disabled={busy} />
          Draft pull request
        </label>
        {error ? (
          <p className={page.error} role="alert">
            {error}
          </p>
        ) : null}
        {note ? (
          <p className="alert alert-success" role="status">
            {note} <Link href={`/s/${sessionId}`}>Open the session</Link>
          </p>
        ) : null}
        <div className={page.actions}>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? "Sending…" : "Open pull request"}
          </button>
        </div>
      </form>

      {list.data === null ? (
        list.error ? (
          <ErrorState title="The pull requests did not load" message={list.error} onRetry={list.reload} />
        ) : (
          <LoadingState label="Loading the pull requests" />
        )
      ) : (
        <>
          {list.error ? (
            <p className="alert alert-warning" role="status">
              {list.error}
            </p>
          ) : null}
          {list.data.length === 0 ? (
            <p className="alert">The agent opened no pull request in this session yet.</p>
          ) : (
            <ul className={page.list} aria-label="Pull requests">
              {list.data.map((pr) => (
                <li key={`${pr.run_id}:${pr.call_id}`}>
                  <PullRequestCard pr={pr} now={now} />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

function PullRequestCard({ pr, now }: { pr: PullRequestOut; now: number }) {
  const url = safeUrl(pr.url);
  const name = pullRequestTitle(pr);
  return (
    <section className={`card ${page.card}`} aria-label={`Pull request: ${name}`}>
      <div className={page.cardHead}>
        <div className={page.name}>
          <span className={toneBadge(pullRequestTone(pr.status))}>{pullRequestStatusLabel(pr.status)}</span>
          {url ? (
            <a href={url} target="_blank" rel="noopener noreferrer">
              {name}
            </a>
          ) : (
            <span>{name}</span>
          )}
        </div>
        <span className={page.meta}>{relativeTime(pr.created_at, now)}</span>
      </div>
      <p className={page.meta}>
        {[pr.repo, pr.head && pr.base ? `${pr.head} → ${pr.base}` : (pr.head ?? pr.base)].filter(Boolean).join(" · ")}
      </p>
      {pr.status === "failed" && pr.output ? <p className={page.error}>{pr.output}</p> : null}
      {pr.status !== "failed" && pr.output ? (
        <details>
          <summary className={styles.summary}>Result</summary>
          <pre className={page.pre}>{pr.output}</pre>
        </details>
      ) : null}
    </section>
  );
}
