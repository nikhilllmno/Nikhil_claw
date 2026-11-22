"use client";

// The sub-agent library (roadmap M7.4): the list of the sub-agents that a workspace can use,
// with a filter by scope, a form to make one, and a view to edit, test, and revert one. It
// takes the org and the workspace as props and loads its own data, so that the settings
// console can use it too.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type LibraryAgent,
  type SubagentCatalog,
  type SubagentScope,
  filterAgents,
  limitsText,
  modelLabel,
  SCOPE_LABELS,
  SCOPE_ORDER,
  usedByText,
} from "@/lib/subagents";
import { useApi } from "./AuthProvider";
import { SubagentEditor } from "./SubagentEditor";
import { SubagentTestRun } from "./SubagentTestRun";
import { SubagentVersions } from "./SubagentVersions";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

type Open = { kind: "agent"; id: string } | { kind: "new" } | null;

export function SubagentLibrary({
  orgId,
  workspaceId,
  newLabel = "New sub-agent",
}: {
  orgId: string;
  workspaceId: string;
  /** The label of the button that makes a sub-agent. The console names the library in it. */
  newLabel?: string;
}) {
  const api = useApi();
  const [open, setOpen] = useState<Open>(null);
  // The control that the list gives the focus to when it shows again: "new" or an agent id.
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const [scope, setScope] = useState<SubagentScope | "">("");
  const [text, setText] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(() => api.listSubagents(orgId, workspaceId), [api, orgId, workspaceId]);
  const catalog = useLoad(load, "Could not load the sub-agents.");

  const back = (message: string | null) => {
    setOpen(null);
    setNotice(message);
    catalog.reload();
  };

  // The saved agent goes into the list at once. The reload then puts it in order.
  const put = (saved: LibraryAgent) => {
    const current = catalog.data;
    if (current === null) return;
    const exists = current.agents.some((a) => a.id === saved.id);
    catalog.setData({
      ...current,
      agents: exists ? current.agents.map((a) => (a.id === saved.id ? saved : a)) : [...current.agents, saved],
    });
    catalog.reload();
  };

  const agent =
    open?.kind === "agent" ? (catalog.data?.agents.find((a) => a.id === open.id) ?? null) : null;

  return (
    <>
      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      {catalog.error ? (
        <ErrorState title="The sub-agents did not load" message={catalog.error} onRetry={catalog.reload} />
      ) : catalog.data === null ? (
        <LoadingState label="Loading the sub-agents" />
      ) : open?.kind === "new" ? (
        <section className={`card ${styles.card}`} aria-label="New sub-agent">
          <h2 className={styles.heading}>New sub-agent</h2>
          <SubagentEditor
            orgId={orgId}
            workspaceId={workspaceId}
            options={catalog.data.options}
            writableScopes={catalog.data.writable_scopes}
            agent={null}
            onCancel={() => setOpen(null)}
            onSaved={(saved) => {
              setNotice(`The sub-agent ${saved.name} is created.`);
              setFocusKey(saved.id);
              put(saved);
              setOpen({ kind: "agent", id: saved.id });
            }}
          />
        </section>
      ) : open?.kind === "agent" ? (
        agent === null ? (
          <>
            <BackButton onBack={() => back(null)} />
            <p className="alert">This sub-agent does not exist, or you cannot see it.</p>
          </>
        ) : (
          <AgentView
            key={agent.id}
            orgId={orgId}
            workspaceId={workspaceId}
            catalog={catalog.data}
            agent={agent}
            onBack={() => back(null)}
            onChanged={(message, changed) => {
              setNotice(message);
              setFocusKey(changed.id);
              put(changed);
            }}
            onDeleted={() => back(`The sub-agent ${agent.name} is deleted.`)}
          />
        )
      ) : (
        <Home
          catalog={catalog.data}
          scope={scope}
          onScope={setScope}
          text={text}
          onText={setText}
          focusKey={focusKey}
          newLabel={newLabel}
          onNew={() => {
            setNotice(null);
            setFocusKey("new");
            setOpen({ kind: "new" });
          }}
          onOpen={(a) => {
            setNotice(null);
            setFocusKey(a.id);
            setOpen({ kind: "agent", id: a.id });
          }}
        />
      )}
    </>
  );
}

function Home({
  catalog,
  scope,
  onScope,
  text,
  onText,
  focusKey,
  newLabel,
  onNew,
  onOpen,
}: {
  catalog: SubagentCatalog;
  scope: SubagentScope | "";
  onScope: (scope: SubagentScope | "") => void;
  text: string;
  onText: (text: string) => void;
  focusKey: string | null;
  newLabel: string;
  onNew: () => void;
  onOpen: (agent: LibraryAgent) => void;
}) {
  const newButton = useRef<HTMLButtonElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const rows = useRef(new Map<string, HTMLButtonElement>());
  const registerRow = useCallback((id: string, button: HTMLButtonElement | null) => {
    if (button === null) rows.current.delete(id);
    else rows.current.set(id, button);
  }, []);

  // The list shows again after a view or the new form closes. The focus goes back to the
  // control that opened it, or to the heading if that control is gone.
  useEffect(() => {
    if (focusKey === null) return;
    const target = focusKey === "new" ? newButton.current : rows.current.get(focusKey);
    (target ?? heading.current)?.focus();
    // Only when the list mounts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const shown = filterAgents(catalog.agents, scope === "" ? null : scope, text);
  return (
    <>
      <h2 ref={heading} tabIndex={-1} className="sr-only">
        All sub-agents
      </h2>
      <div className={styles.toolbar}>
        <label className={`field ${styles.grow}`}>
          <span className="label">Filter</span>
          <input
            className="input"
            type="search"
            value={text}
            placeholder="Name or description"
            onChange={(e) => onText(e.target.value)}
          />
        </label>
        <label className="field">
          <span className="label">Who can use it</span>
          <select className="input" value={scope} onChange={(e) => onScope(e.target.value as SubagentScope | "")}>
            <option value="">All</option>
            {SCOPE_ORDER.map((s) => (
              <option key={s} value={s}>
                {SCOPE_LABELS[s]}
              </option>
            ))}
          </select>
        </label>
        {catalog.writable_scopes.length > 0 ? (
          <button ref={newButton} type="button" className="btn btn-primary" onClick={onNew}>
            {newLabel}
          </button>
        ) : null}
      </div>
      {catalog.agents.length === 0 ? (
        <p className="alert">
          This workspace has no sub-agents yet.{" "}
          {catalog.writable_scopes.length > 0
            ? `Make one with ${newLabel}.`
            : "Ask an admin to make one."}
        </p>
      ) : shown.length === 0 ? (
        <p className={styles.meta}>No sub-agent matches the filter.</p>
      ) : (
        <ul className={styles.list} aria-label="Sub-agents">
          {shown.map((a) => {
            const limits = limitsText(a);
            return (
              <li key={a.id}>
                <section className={`card ${styles.card}`} aria-label={a.name}>
                  <div className={styles.cardHead}>
                    <h2 className={styles.heading}>{a.name}</h2>
                    <div className={styles.name}>
                      <span className="badge badge-accent">{SCOPE_LABELS[a.scope]}</span>
                      <span className={styles.meta}>v{a.version}</span>
                    </div>
                  </div>
                  <p className={styles.text}>{a.description}</p>
                  <p className={styles.meta}>
                    Model: {modelLabel(a.model)}
                    {a.toolsets.length > 0 ? ` · Tools: ${a.toolsets.join(", ")}` : ""}
                    {limits ? ` · Limits: ${limits}` : ""}
                  </p>
                  <p className={styles.meta}>{usedByText(a.used_by)}</p>
                  <div className={styles.actions}>
                    <button
                      ref={(button) => registerRow(a.id, button)}
                      type="button"
                      className="btn btn-small"
                      onClick={() => onOpen(a)}
                    >
                      {a.can_edit ? "Open and edit" : "Open"}
                      <span className="sr-only"> {a.name}</span>
                    </button>
                  </div>
                </section>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

function BackButton({ onBack }: { onBack: () => void }) {
  return (
    <div className={styles.actions}>
      <button type="button" className="btn btn-quiet btn-small" onClick={onBack}>
        All sub-agents
      </button>
    </div>
  );
}

function AgentView({
  orgId,
  workspaceId,
  catalog,
  agent,
  onBack,
  onChanged,
  onDeleted,
}: {
  orgId: string;
  workspaceId: string;
  catalog: SubagentCatalog;
  agent: LibraryAgent;
  onBack: () => void;
  onChanged: (message: string, agent: LibraryAgent) => void;
  onDeleted: () => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  return (
    <>
      <BackButton onBack={onBack} />
      <section className={`card ${styles.card}`} aria-label={agent.name}>
        <div className={styles.cardHead}>
          <h2 ref={heading} tabIndex={-1} className={styles.heading}>
            {agent.name}
          </h2>
          <div className={styles.name}>
            <span className="badge badge-accent">{SCOPE_LABELS[agent.scope]}</span>
            <span className={styles.meta}>v{agent.version}</span>
          </div>
        </div>
        <p className={styles.meta}>{usedByText(agent.used_by)}</p>
        <SubagentEditor
          // A save gives the form a new start from the saved values.
          key={agent.version}
          orgId={orgId}
          workspaceId={workspaceId}
          options={catalog.options}
          writableScopes={catalog.writable_scopes}
          agent={agent}
          onCancel={onBack}
          onSaved={(saved) => onChanged(`The sub-agent ${saved.name} is saved. Version ${saved.version}.`, saved)}
          onDeleted={onDeleted}
        />
        <SubagentVersions
          orgId={orgId}
          agent={agent}
          onReverted={(reverted) =>
            onChanged(`The sub-agent ${reverted.name} is reverted. Version ${reverted.version}.`, reverted)
          }
        />
        <SubagentTestRun orgId={orgId} workspaceId={workspaceId} agent={agent} />
      </section>
    </>
  );
}
