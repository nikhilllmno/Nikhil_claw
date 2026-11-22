"use client";

// The code of a session next to the chat, as in Codex web: the changes as diffs, the commands
// and test runs, the repos, the pull requests, and the checkpoints (M7.9 parts, M8.2 part C).
// At a wide width it is a column next to the chat. At a narrow width it covers the chat as a
// dialog. The full Code page stays at `/s/{id}/code`.

import Link from "next/link";
import { useCallback, useId, useRef } from "react";
import { useApi } from "./AuthProvider";
import { Checkpoints } from "./Checkpoints";
import styles from "./CodePanel.module.css";
import { DiffViewer } from "./DiffViewer";
import { PullRequests } from "./PullRequests";
import { RepoAttach } from "./RepoAttach";
import { TestRuns } from "./TestRuns";
import { useDrawerFocus } from "./useDrawerFocus";
import { usePolled } from "./usePolled";

export const CODE_TABS = [
  { id: "changes", label: "Changes" },
  { id: "tests", label: "Tests" },
  { id: "repos", label: "Repos" },
  { id: "prs", label: "Pull request" },
  { id: "checkpoints", label: "Checkpoints" },
] as const;

export type CodeTab = (typeof CODE_TABS)[number]["id"];

export function CodePanel({
  sessionId,
  tab,
  onTab,
  onClose,
  modal,
  onRequested,
}: {
  sessionId: string;
  tab: CodeTab;
  onTab: (tab: CodeTab) => void;
  onClose: () => void;
  /** True at a narrow width: the panel covers the chat. */
  modal: boolean;
  /** Called after a request for a pull request: the agent works on it in the chat. */
  onRequested?: () => void;
}) {
  const api = useApi();
  const baseId = useId();
  const loadCoding = useCallback(() => api.getCoding(sessionId), [api, sessionId]);
  const coding = usePolled(loadCoding, "Could not load the repos.");
  const panel = useRef<HTMLElement>(null);
  const tabRefs = useRef<Partial<Record<CodeTab, HTMLButtonElement | null>>>({});

  // As a dialog: the focus moves in, Tab stays in, Esc closes it, and the focus goes back.
  useDrawerFocus(modal, panel, onClose);

  const onTabKey = (event: React.KeyboardEvent) => {
    const index = CODE_TABS.findIndex((t) => t.id === tab);
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % CODE_TABS.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + CODE_TABS.length) % CODE_TABS.length;
    else return;
    event.preventDefault();
    const id = CODE_TABS[next]?.id;
    if (id === undefined) return;
    onTab(id);
    tabRefs.current[id]?.focus();
  };

  const repos = coding.data?.repos ?? [];
  return (
    <aside
      ref={panel}
      className={`${styles.panel} ${modal ? styles.modal : ""}`}
      aria-label="Code of the session"
      role={modal ? "dialog" : undefined}
      aria-modal={modal ? true : undefined}
    >
      <header className={styles.head}>
        <div className={styles.headText}>
          <h2 className={styles.title}>Code</h2>
          <p className={styles.meta}>
            {repos.length === 0
              ? "No repo attached"
              : repos.map((r) => `${r.repo}${r.ref ? `@${r.ref}` : ""}`).join(", ")}
          </p>
        </div>
        <Link href={`/s/${sessionId}/code`} className="btn btn-quiet btn-small">
          Full page
        </Link>
        <button type="button" className="btn btn-quiet btn-small" onClick={onClose} aria-label="Close the code panel">
          Close
        </button>
      </header>
      <div className={styles.tabs} role="tablist" aria-label="Code sections" onKeyDown={onTabKey}>
        {CODE_TABS.map((t) => (
          <button
            key={t.id}
            ref={(el) => {
              tabRefs.current[t.id] = el;
            }}
            type="button"
            role="tab"
            id={`${baseId}-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`${baseId}-panel`}
            tabIndex={tab === t.id ? 0 : -1}
            className={`${styles.tab} ${tab === t.id ? styles.tabSelected : ""}`}
            onClick={() => onTab(t.id)}
          >
            {t.label}
            {t.id === "repos" && repos.length > 0 ? <span className="badge">{repos.length}</span> : null}
          </button>
        ))}
      </div>
      <div className={styles.body} role="tabpanel" id={`${baseId}-panel`} aria-labelledby={`${baseId}-tab-${tab}`}>
        {tab === "changes" ? <DiffViewer sessionId={sessionId} /> : null}
        {tab === "tests" ? <TestRuns sessionId={sessionId} /> : null}
        {tab === "repos" ? (
          coding.data !== null ? (
            <RepoAttach sessionId={sessionId} repos={repos} onChanged={coding.reload} />
          ) : coding.error ? (
            <p className="alert alert-error" role="alert">
              {coding.error}{" "}
              <button type="button" className="btn btn-small" onClick={coding.reload}>
                Try again
              </button>
            </p>
          ) : (
            <div className="skeleton" style={{ height: "var(--space-8)" }} aria-busy="true" />
          )
        ) : null}
        {tab === "prs" ? <PullRequests sessionId={sessionId} onRequested={onRequested} /> : null}
        {tab === "checkpoints" ? <Checkpoints sessionId={sessionId} /> : null}
      </div>
    </aside>
  );
}
