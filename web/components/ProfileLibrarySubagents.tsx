"use client";

// The library sub-agents of an agent profile (roadmap M7.4). The section shows the agents of the
// library that the profile uses, by name, and lets an editor add and remove them. The profile
// form saves the list. A session freezes its sub-agents when it starts, so a change applies to
// the next session.

import { useCallback, useState } from "react";
import Link from "next/link";
import {
  type LibraryAgent,
  SCOPE_LABELS,
  missingToolsets,
  takenNames,
} from "@/lib/subagents";
import { useApi } from "./AuthProvider";
import { LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./AgentSettings.module.css";
import page from "./Page.module.css";

export function ProfileLibrarySubagents({
  orgId,
  workspaceId,
  ids,
  onChange,
  readOnly,
  supported,
  inlineNames,
  toolsets,
  total,
  max,
}: {
  orgId: string;
  /** The workspace of the profile, or the workspace that the user has open. */
  workspaceId: string | null;
  /** The ids of the library agents of the profile, in order. */
  ids: string[];
  onChange: (ids: string[]) => void;
  readOnly: boolean;
  /** False when the runtime of the profile has no sub-agents. */
  supported: boolean;
  /** The names of the inline sub-agents. A library agent cannot have the same name. */
  inlineNames: string[];
  /** The tool groups of the profile. */
  toolsets: string[];
  /** The number of inline and library sub-agents together. */
  total: number;
  /** The most that a profile can have, inline and library together. */
  max: number;
}) {
  const api = useApi();
  const load = useCallback(() => api.listSubagents(orgId, workspaceId), [api, orgId, workspaceId]);
  const catalog = useLoad(load, "Could not load the library.");
  const [choice, setChoice] = useState("");

  const agents = catalog.data?.agents ?? null;
  const taken = takenNames(agents ?? [], ids, inlineNames);
  const available = (agents ?? []).filter((a) => !ids.includes(a.id));
  const full = total >= max;
  const selected = available.some((a) => a.id === choice) ? choice : "";

  const add = () => {
    if (selected === "") return;
    onChange([...ids, selected]);
    setChoice("");
  };

  const find = (id: string): LibraryAgent | null => agents?.find((a) => a.id === id) ?? null;

  return (
    <section className={`card ${styles.section}`} aria-labelledby="library-heading">
      <div className={styles.row}>
        <h2 className={styles.heading} id="library-heading">
          Library sub-agents
        </h2>
        <Link href="/subagents" className="btn btn-small">
          Open the library
        </Link>
      </div>
      <p className="hint">
        Helpers that you make once and share. The agent can hand work to them. A change to a library
        sub-agent applies from the next session. This agent has {total} of {max} sub-agents, from the
        library and in the agent together.
      </p>
      {!supported ? (
        <p className="hint">The runtime of this agent does not support sub-agents.</p>
      ) : null}
      {supported && ids.length > 0 && !toolsets.includes("subagents") ? (
        <p className="alert alert-warning" role="note">
          Turn on the tool group subagents. The agent needs it to use a sub-agent.
        </p>
      ) : null}
      {catalog.error ? (
        <div className="alert alert-error" role="alert">
          <span>{catalog.error}</span>{" "}
          <button type="button" className="btn btn-small" onClick={catalog.reload}>
            Retry
          </button>
        </div>
      ) : null}
      {catalog.data === null && !catalog.error ? (
        <LoadingState label="Loading the library" />
      ) : ids.length === 0 ? (
        <p className="hint">No library sub-agents.</p>
      ) : (
        <ul className={styles.subagents} aria-label="Library sub-agents of this agent">
          {ids.map((id) => {
            const agent = find(id);
            const missing = agent ? missingToolsets(agent, toolsets) : [];
            return (
              <li key={id} className={styles.subagent}>
                <div className={styles.row}>
                  <h3 className={styles.subTitle}>
                    {agent ? agent.name : catalog.data ? "A sub-agent that is not in this list" : "…"}
                  </h3>
                  <div className={page.actions}>
                    {agent ? <span className="badge badge-accent">{SCOPE_LABELS[agent.scope]}</span> : null}
                    {!readOnly ? (
                      <button
                        type="button"
                        className="btn btn-small btn-danger"
                        onClick={() => onChange(ids.filter((x) => x !== id))}
                      >
                        Remove
                        {agent ? <span className="sr-only"> {agent.name}</span> : null}
                      </button>
                    ) : null}
                  </div>
                </div>
                {agent ? (
                  <p className="hint">{agent.description}</p>
                ) : catalog.data ? (
                  <p className="hint">
                    Someone deleted it, or it is in another workspace, or you cannot see it. Remove it, or
                    ask the owner.
                  </p>
                ) : null}
                {missing.length > 0 ? (
                  <p className="alert alert-warning" role="note">
                    It needs the tool groups {missing.join(", ")}. Turn them on for this agent.
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      {!readOnly && supported && catalog.data !== null ? (
        available.length === 0 ? (
          <p className="hint">
            {agents !== null && agents.length === 0
              ? "The library has no sub-agents for this workspace yet."
              : "Every sub-agent of the library is in this agent."}
          </p>
        ) : (
          <div className={page.toolbar}>
            <label className={`field ${page.grow}`}>
              <span className="label">Add from the library</span>
              <select
                className="input"
                value={selected}
                disabled={full}
                onChange={(e) => setChoice(e.target.value)}
              >
                <option value="">Choose a sub-agent</option>
                {available.map((a) => (
                  <option key={a.id} value={a.id} disabled={taken.has(a.name)}>
                    {a.name} ({SCOPE_LABELS[a.scope]}){taken.has(a.name) ? " - name is in use" : ""}
                  </option>
                ))}
              </select>
              {full ? <span className="hint">The agent has the most sub-agents that it can have.</span> : null}
            </label>
            <button type="button" className="btn btn-small" disabled={selected === "" || full} onClick={add}>
              Add
            </button>
          </div>
        )
      ) : null}
    </section>
  );
}
