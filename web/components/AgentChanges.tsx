"use client";

// The change history of all the agents of the org (docs/architecture.md, Sections 11.4 and
// 17.6, M6.1 and M6.9), newest first. The revert is on the page of each agent.

import Link from "next/link";
import { useCallback, useState } from "react";
import { mergeChanges } from "@/lib/admin";
import { changeTitle } from "@/lib/profileHistory";
import { relativeTime } from "@/lib/time";
import { useAdminOrg } from "./Admin";
import { useApi } from "./AuthProvider";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import cards from "./Cards.module.css";
import styles from "./Page.module.css";
import own from "./Admin.module.css";

export function AgentChanges() {
  const org = useAdminOrg();
  const api = useApi();
  const profiles = org.agent_profiles;
  const load = useCallback(
    async () =>
      mergeChanges(
        await Promise.all(
          profiles.map(async (p) => ({
            profileId: p.id,
            profileName: p.name,
            changes: await api.listAgentProfileChanges(org.id, p.id),
          })),
        ),
      ),
    [api, org.id, profiles],
  );
  const { data: changes, error, reload } = useLoad(load, "Could not load the changes.");
  const [now] = useState(() => Date.now());

  return (
    <section className={`card ${styles.card}`} aria-label="Changes of the agents">
      <p className={styles.text}>
        Each edit, approved proposal, and revert of the soul, the settings, the tools, and the policy rules of the agents
        of {org.name}. Open an agent to revert a change.
      </p>
      {error ? (
        <ErrorState title="The changes did not load" message={error} onRetry={reload} />
      ) : changes === null ? (
        <LoadingState label="Loading the changes" />
      ) : changes.length === 0 ? (
        <p className={styles.meta}>No changes yet. A change to an agent shows here.</p>
      ) : (
        <ul className={styles.entries}>
          {changes.map((change) => (
            <li key={change.id} className={own.event}>
              <div className={own.eventHead}>
                <span className={own.eventTitle}>
                  <Link href={`/agents/${change.profile_id}`}>{change.profile_name}</Link> · {changeTitle(change)}
                </span>
                <time className="hint" dateTime={change.created_at} title={new Date(change.created_at).toLocaleString()}>
                  {relativeTime(change.created_at, now)}
                </time>
              </div>
              {change.summary ? <p className={styles.text}>{change.summary}</p> : null}
              <div className={own.chips}>
                {change.reverted_by_change_id !== null ? <span className="badge">Reverted</span> : null}
                {change.redacted ? <span className="badge badge-warning">Had a secret</span> : null}
              </div>
              {change.diff ? (
                <details>
                  <summary className={`hint ${own.summary}`}>Show the diff</summary>
                  <pre className={cards.code}>{change.diff}</pre>
                </details>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
