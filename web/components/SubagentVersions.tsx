"use client";

// The version history of a library sub-agent, newest first (roadmap M7.4). A user who can edit
// the agent can revert to an old version. A revert makes a new version.

import { useCallback, useState } from "react";
import { errorText } from "@/lib/api";
import { type LibraryAgent, type LibraryVersion, specText, versionSource } from "@/lib/subagents";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import page from "./Page.module.css";

export function SubagentVersions({
  orgId,
  agent,
  onReverted,
}: {
  orgId: string;
  agent: LibraryAgent;
  onReverted: (agent: LibraryAgent) => void;
}) {
  const api = useApi();
  // The version is a dependency, so a save or a revert loads the history again.
  const load = useCallback(
    () => api.listSubagentVersions(orgId, agent.id),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [api, orgId, agent.id, agent.version],
  );
  const versions = useLoad(load, "Could not load the versions.");
  const [shown, setShown] = useState<number | null>(null);
  const [confirming, setConfirming] = useState<number | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now] = useState(() => Date.now());

  const revert = async (version: LibraryVersion) => {
    setBusy(version.version);
    setError(null);
    try {
      const result = await api.revertSubagent(orgId, agent.id, version.version);
      setConfirming(null);
      onReverted(result);
    } catch (e) {
      setConfirming(null);
      setError(errorText(e, "The sub-agent was not reverted."));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className={page.section}>
      <h3 className={page.subheading}>Versions</h3>
      <p className={page.meta}>
        Each save makes a new version. A revert writes the old values as a new version. An agent
        that uses this sub-agent gets the change in its next session.
      </p>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      {versions.error ? (
        <ErrorState title="The versions did not load" message={versions.error} onRetry={versions.reload} />
      ) : versions.data === null ? (
        <LoadingState label="Loading the versions" />
      ) : versions.data.length === 0 ? (
        <p className={page.meta}>No versions yet.</p>
      ) : (
        <ul className={page.list} aria-label="Versions">
          {versions.data.map((v) => {
            const current = v.version === agent.version;
            return (
              <li key={v.version} className={page.form}>
                <div className={page.row}>
                  <div className={page.name}>
                    <span>Version {v.version}</span>
                    {current ? <span className="badge badge-accent">Current</span> : null}
                    <span className={page.meta}>
                      {versionSource(v)} ·{" "}
                      <time dateTime={v.created_at} title={new Date(v.created_at).toLocaleString()}>
                        {relativeTime(v.created_at, now)}
                      </time>
                    </span>
                  </div>
                  <div className={page.actions}>
                    <button
                      type="button"
                      className="btn btn-quiet btn-small"
                      aria-expanded={shown === v.version}
                      onClick={() => setShown(shown === v.version ? null : v.version)}
                    >
                      {shown === v.version ? "Hide" : "Show"}
                      <span className="sr-only"> version {v.version}</span>
                    </button>
                    {agent.can_edit && !current ? (
                      confirming === v.version ? (
                        <>
                          <button
                            type="button"
                            className="btn btn-danger btn-small"
                            disabled={busy !== null}
                            onClick={() => void revert(v)}
                          >
                            {busy === v.version ? "Reverting…" : "Confirm revert"}
                          </button>
                          <button
                            type="button"
                            className="btn btn-quiet btn-small"
                            disabled={busy !== null}
                            onClick={() => setConfirming(null)}
                          >
                            Cancel
                          </button>
                        </>
                      ) : (
                        <button
                          type="button"
                          className="btn btn-small"
                          disabled={busy !== null}
                          onClick={() => {
                            setError(null);
                            setConfirming(v.version);
                          }}
                        >
                          Revert
                          <span className="sr-only"> to version {v.version}</span>
                        </button>
                      )
                    ) : null}
                  </div>
                </div>
                {shown === v.version ? <pre className={page.pre}>{specText(v.spec)}</pre> : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
