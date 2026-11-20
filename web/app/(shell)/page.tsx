"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useApi } from "@/components/AuthProvider";
import { useData } from "@/components/DataProvider";
import { ErrorState, LoadingState } from "@/components/ui";
import type { Audience } from "@/lib/api";
import styles from "./home.module.css";

export default function HomePage() {
  const api = useApi();
  const router = useRouter();
  const { me, meError, reloadMe, workspaces, workspaceId, setWorkspaceId, profilesFor, reloadSessions } =
    useData();
  const profiles = profilesFor(workspaceId);
  const [chosenProfile, setProfileId] = useState("");
  const [audience, setAudience] = useState<Audience>("private");
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The chosen agent, or the first one if the workspace changed and does not have it.
  const profileId = profiles.some((p) => p.id === chosenProfile) ? chosenProfile : (profiles[0]?.id ?? "");

  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading your workspaces" />;
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

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!workspaceId || !profileId) return;
    setBusy(true);
    setError(null);
    try {
      const session = await api.createSession({
        workspace_id: workspaceId,
        agent_profile_id: profileId,
        audience,
        title: title.trim() || null,
      });
      reloadSessions();
      router.push(`/s/${session.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the session.");
      setBusy(false);
    }
  };

  return (
    <main className={styles.page}>
      <form className={styles.panel} onSubmit={submit}>
        <h1 className={styles.title}>What should we work on?</h1>
        <p className={styles.lead}>Pick an agent and start a session. You can send the first message next.</p>
        <label className="field">
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
        <label className="field">
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
          {profiles.length === 0 ? (
            <span className="hint">This workspace has no agent. Ask an admin to add one.</span>
          ) : null}
        </label>
        <label className="field">
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
        <label className="field">
          <span className="label">Title (optional)</span>
          <input
            className="input"
            value={title}
            maxLength={200}
            onChange={(e) => setTitle(e.target.value)}
            disabled={busy}
          />
        </label>
        {error ? (
          <p className="alert alert-error" role="alert">
            {error}
          </p>
        ) : null}
        <button type="submit" className="btn btn-primary" disabled={busy || !profileId}>
          {busy ? "Starting…" : "Start session"}
        </button>
      </form>
    </main>
  );
}
