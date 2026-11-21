"use client";

// The Code page of a session (M7.9): repos, plan mode, file changes as diffs, commands and test
// runs, checkpoints, and pull requests. Each section is its own component. The panel loads the
// coding state (it reads again every 5 seconds) and gives it to the sections that need it.

import { useCallback, useId, useRef, useState } from "react";
import { sessionTitle } from "@/lib/chat";
import { proposedPlan } from "@/lib/coding";
import { useApi } from "./AuthProvider";
import { Checkpoints } from "./Checkpoints";
import styles from "./Coding.module.css";
import { DiffViewer } from "./DiffViewer";
import { PageHeader } from "./PageHeader";
import page from "./Page.module.css";
import { PlanPanel } from "./PlanPanel";
import { PullRequests } from "./PullRequests";
import { RepoAttach } from "./RepoAttach";
import { TestRuns } from "./TestRuns";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import { usePolled } from "./usePolled";

const TABS = [
  { id: "repos", label: "Repos" },
  { id: "plan", label: "Plan" },
  { id: "changes", label: "Changes" },
  { id: "tests", label: "Commands and tests" },
  { id: "checkpoints", label: "Checkpoints" },
  { id: "prs", label: "Pull requests" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export function CodingPanel({ sessionId }: { sessionId: string }) {
  const api = useApi();
  const baseId = useId();
  const loadSession = useCallback(() => api.getSession(sessionId), [api, sessionId]);
  const loadCoding = useCallback(() => api.getCoding(sessionId), [api, sessionId]);
  const session = useLoad(loadSession, "Could not load the session.");
  const coding = usePolled(loadCoding, "Could not load the coding state.");
  // The tab that the user chose. Before that: Plan, or Repos when no repo is attached.
  const [chosen, setChosen] = useState<TabId | null>(null);
  const tabRefs = useRef<Partial<Record<TabId, HTMLButtonElement | null>>>({});

  const error = session.error ?? (coding.data === null ? coding.error : null);
  if (error) {
    return (
      <ErrorState
        title="The Code page did not load"
        message={error}
        onRetry={() => {
          session.reload();
          coding.reload();
        }}
      />
    );
  }
  if (session.data === null || coding.data === null) return <LoadingState label="Loading the Code page" />;

  const tab: TabId = chosen ?? (coding.data.repos.length === 0 ? "repos" : "plan");
  const waiting = proposedPlan(coding.data.plan) !== null;

  const onKeyDown = (event: React.KeyboardEvent) => {
    const index = TABS.findIndex((t) => t.id === tab);
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + TABS.length) % TABS.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = TABS.length - 1;
    else return;
    event.preventDefault();
    const id = TABS[next]?.id;
    if (id === undefined) return;
    setChosen(id);
    tabRefs.current[id]?.focus();
  };

  return (
    <div className={page.page}>
      <PageHeader
        title="Code"
        back={{ href: `/s/${sessionId}`, label: "Back to the session" }}
        meta={
          <>
            {sessionTitle(session.data)} · Attach a repo, plan the work, review the changes, and open a pull
            request.
          </>
        }
      />
      {coding.error ? (
        <p className="alert alert-warning" role="status">
          {coding.error}
        </p>
      ) : null}
      <div className={styles.tabs} role="tablist" aria-label="Code sections" onKeyDown={onKeyDown}>
        {TABS.map((t) => (
          <button
            key={t.id}
            ref={(el) => {
              tabRefs.current[t.id] = el;
            }}
            type="button"
            role="tab"
            id={`${baseId}-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`${baseId}-panel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            className={`${styles.tab} ${tab === t.id ? styles.tabSelected : ""}`}
            onClick={() => setChosen(t.id)}
          >
            {t.label}
            {t.id === "plan" && waiting ? <span className="badge badge-warning">Waiting for you</span> : null}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id={`${baseId}-panel-${tab}`}
        aria-labelledby={`${baseId}-tab-${tab}`}
      >
        {tab === "repos" ? (
          <RepoAttach sessionId={sessionId} repos={coding.data.repos} onChanged={coding.reload} />
        ) : null}
        {tab === "plan" ? <PlanPanel sessionId={sessionId} coding={coding.data} onChanged={coding.reload} /> : null}
        {tab === "changes" ? <DiffViewer sessionId={sessionId} /> : null}
        {tab === "tests" ? <TestRuns sessionId={sessionId} /> : null}
        {tab === "checkpoints" ? <Checkpoints sessionId={sessionId} /> : null}
        {tab === "prs" ? <PullRequests sessionId={sessionId} /> : null}
      </div>
    </div>
  );
}
