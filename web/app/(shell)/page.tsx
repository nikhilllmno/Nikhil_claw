"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useApi } from "@/components/AuthProvider";
import { useData } from "@/components/DataProvider";
import { ErrorState, LoadingState } from "@/components/ui";
import { Welcome } from "@/components/Welcome";
import { type Audience, errorText, type Org } from "@/lib/api";
import { startPlan, taskTitle } from "@/lib/newTask";
import { currentStep, loadProgress, onboardingSteps, saveProgress, showSetupCard } from "@/lib/onboarding";
import styles from "./home.module.css";

export default function HomePage() {
  const api = useApi();
  const router = useRouter();
  const {
    me,
    meError,
    reloadMe,
    workspaces,
    workspaceId,
    setWorkspaceId,
    profilesFor,
    orgOfWorkspace,
    reloadSessions,
  } = useData();
  const profiles = profilesFor(workspaceId);
  const [chosenProfile, setProfileId] = useState("");
  const [audience, setAudience] = useState<Audience>("private");
  const [task, setTask] = useState("");
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("");
  const [planFirst, setPlanFirst] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A session that started, but a later step (the repo or the first message) failed.
  const [started, setStarted] = useState<string | null>(null);

  // The chosen agent, or the first one if the workspace changed and does not have it.
  const profileId = profiles.some((p) => p.id === chosenProfile) ? chosenProfile : (profiles[0]?.id ?? "");

  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading your workspaces" />;
  // A user without an org: invites, and sign-up (docs/architecture.md, Section 17.2).
  if (me.orgs.length === 0) return <Welcome />;
  if (workspaces.length === 0) {
    return (
      <main className={styles.page}>
        <div className="state">
          <h2>No workspace yet</h2>
          <p>You are not in a workspace. Ask an admin of your org to add you, then reload.</p>
        </div>
      </main>
    );
  }

  // One flow, as in Codex: make the session, attach the repo, send the task, open the chat.
  const submit = async (event?: React.FormEvent) => {
    event?.preventDefault();
    if (!workspaceId || !profileId || busy) return;
    const steps = startPlan(task, repo, branch, planFirst);
    setBusy(true);
    setError(null);
    setStarted(null);
    let sessionId: string | null = null;
    try {
      const session = await api.createSession({
        workspace_id: workspaceId,
        agent_profile_id: profileId,
        audience,
        title: taskTitle(steps.task),
      });
      sessionId = session.id;
      reloadSessions();
      if (steps.repo !== null) await api.attachRepo(session.id, steps.repo);
      if (steps.first === "plan") await api.startPlan(session.id, steps.task);
      else if (steps.first === "message") await api.sendMessage(session.id, steps.task);
      // The run exists now: the sidebar shows the session as working.
      reloadSessions();
      router.push(`/s/${session.id}`);
    } catch (e) {
      setError(errorText(e, sessionId === null ? "Could not start the session." : "The session started, but a step failed."));
      setStarted(sessionId);
      setBusy(false);
    }
  };

  return (
    <main className={styles.page}>
      <div className={styles.panel}>
        <SetupCard org={orgOfWorkspace(workspaceId)} />
      <form className={styles.panel} onSubmit={(e) => void submit(e)}>
        <h1 className={styles.title}>What should we work on?</h1>
        <div className={styles.taskBox}>
          <label className="sr-only" htmlFor="task">
            Task
          </label>
          <textarea
            id="task"
            className={`input ${styles.task}`}
            rows={4}
            value={task}
            maxLength={32_000}
            placeholder="Describe a task, ask a question, or leave it empty to start an empty session"
            onChange={(e) => setTask(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void submit();
              }
            }}
            disabled={busy}
          />
          <div className={styles.repoRow}>
            <label className={styles.inline}>
              <span className="sr-only">Repo (optional)</span>
              <input
                className="input"
                value={repo}
                placeholder="Repo (optional): owner/name or a GitHub URL"
                onChange={(e) => setRepo(e.target.value)}
                disabled={busy}
              />
            </label>
            <label className={styles.branch}>
              <span className="sr-only">Branch (optional)</span>
              <input
                className="input"
                value={branch}
                placeholder="Branch"
                onChange={(e) => setBranch(e.target.value)}
                disabled={busy || repo.trim() === ""}
              />
            </label>
          </div>
        </div>
        <div className={styles.options}>
          <label className={styles.option}>
            <span className="label">Workspace</span>
            <select
              className="input"
              value={workspaceId ?? ""}
              onChange={(e) => setWorkspaceId(e.target.value)}
              disabled={busy}
            >
              {workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.label}
                </option>
              ))}
            </select>
          </label>
          <label className={styles.option}>
            <span className="label">Agent</span>
            <select
              className="input"
              value={profileId}
              onChange={(e) => setProfileId(e.target.value)}
              disabled={busy || profiles.length === 0}
              required
            >
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.model})
                </option>
              ))}
            </select>
          </label>
          <label className={styles.option}>
            <span className="label">Who can see it</span>
            <select
              className="input"
              value={audience}
              onChange={(e) => setAudience(e.target.value as Audience)}
              disabled={busy}
            >
              <option value="private">Only me</option>
              <option value="shared">Everyone in the workspace</option>
            </select>
          </label>
        </div>
        {profiles.length === 0 ? <p className="hint">This workspace has no agent. Ask an admin to add one.</p> : null}
        {error ? (
          <p className="alert alert-error" role="alert">
            {error}
            {started ? (
              <>
                {" "}
                <Link href={`/s/${started}`}>Open the session</Link>
              </>
            ) : null}
          </p>
        ) : null}
        <div className={styles.submitRow}>
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={planFirst}
              onChange={(e) => setPlanFirst(e.target.checked)}
              disabled={busy || task.trim() === ""}
            />
            Plan first (the agent changes no files until you approve)
          </label>
          <button type="submit" className="btn btn-primary" disabled={busy || !profileId}>
            {busy ? "Starting…" : task.trim() ? "Start the task" : "Start session"}
          </button>
        </div>
      </form>
      </div>
    </main>
  );
}

/** The card that brings an admin back to the setup guide until it is done (Section 17.6). */
function SetupCard({ org }: { org: Org | null }) {
  const [hidden, setHidden] = useState(false);
  if (org === null || hidden || !org.region_here) return null;
  const progress = loadProgress(window.localStorage, org.id);
  if (!showSetupCard(org, progress)) return null;
  const steps = onboardingSteps(org, progress);
  const done = steps.filter((s) => s.done).length;
  const next = steps.find((s) => s.id === currentStep(steps));
  return (
    <section className={`card ${styles.setup}`} aria-labelledby="setup-card">
      <h2 id="setup-card" className={styles.setupTitle}>
        Finish setting up {org.name}
      </h2>
      <p className={styles.lead}>
        {done} of {steps.length} steps done.{next ? ` Next: ${next.title.toLowerCase()}.` : ""}
      </p>
      <div className={styles.setupActions}>
        <Link href="/onboarding" className="btn btn-primary btn-small">
          Continue the setup
        </Link>
        <button
          type="button"
          className="btn btn-quiet btn-small"
          onClick={() => {
            saveProgress(window.localStorage, org.id, { ...progress, done: true });
            setHidden(true);
          }}
        >
          Hide
        </button>
      </div>
    </section>
  );
}
