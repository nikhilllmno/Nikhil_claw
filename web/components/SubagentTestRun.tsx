"use client";

// The test run of a library sub-agent (roadmap M7.4). The user chooses an agent of the
// workspace and writes a message. The API starts a new private session that runs as the
// sub-agent. The page then opens that session.

import { useState } from "react";
import { useRouter } from "next/navigation";
import { errorText } from "@/lib/api";
import { type LibraryAgent, MAX_TEST_MESSAGE_CHARS, testMessageProblem } from "@/lib/subagents";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import page from "./Page.module.css";

export function SubagentTestRun({
  orgId,
  workspaceId,
  agent,
}: {
  orgId: string;
  workspaceId: string;
  agent: LibraryAgent;
}) {
  const api = useApi();
  const router = useRouter();
  const { profilesFor } = useData();
  const profiles = profilesFor(workspaceId);
  const [profileId, setProfileId] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The first agent is the default. The user can choose another one.
  const chosen = profiles.some((p) => p.id === profileId) ? profileId : (profiles[0]?.id ?? "");
  const problem = testMessageProblem(message);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (problem !== null || chosen === "") return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.testSubagent(orgId, agent.id, {
        workspace_id: workspaceId,
        profile_id: chosen,
        message,
      });
      // The page changes, so the form stays busy until it is gone.
      router.push(`/s/${result.session_id}`);
    } catch (e) {
      setError(errorText(e, "The test run did not start."));
      setBusy(false);
    }
  };

  return (
    <div className={page.section}>
      <h3 className={page.subheading}>Test run</h3>
      <p className={page.meta}>
        Start a new private session that runs as this sub-agent. Choose an agent of this workspace
        and write a message. The session opens when it starts.
      </p>
      {profiles.length === 0 ? (
        <p className="alert">This workspace has no agent yet. Make an agent first.</p>
      ) : (
        <form className={page.form} onSubmit={submit} aria-label={`Test ${agent.name}`}>
          <label className="field">
            <span className="label">Agent</span>
            <select className="input" value={chosen} disabled={busy} onChange={(e) => setProfileId(e.target.value)}>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="label">Message</span>
            <textarea
              className="input"
              rows={4}
              value={message}
              maxLength={MAX_TEST_MESSAGE_CHARS}
              disabled={busy}
              onChange={(e) => setMessage(e.target.value)}
            />
          </label>
          {error ? (
            <p className="alert alert-error" role="alert">
              {error}
            </p>
          ) : null}
          <div className={page.actions}>
            <button type="submit" className="btn btn-primary btn-small" disabled={busy || problem !== null}>
              {busy ? "Starting…" : "Start test run"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
