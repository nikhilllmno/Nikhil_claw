"use client";

// Small pieces that every section of the console uses: the head of a section, the choice of an
// agent, the editor of one agent, and the card of a part that another milestone adds.

import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentProfile, AgentProfileSummary, Org } from "@/lib/api";
import { canManageAgents, sectionById, type SectionId } from "@/lib/console";
import { useApi } from "../AuthProvider";
import { useData } from "../DataProvider";
import { ErrorState, LoadingState } from "../ui";
import { useLoad } from "../useLoad";
import styles from "../Page.module.css";
import { useConsole } from "./Console";

/** The head of a section: the title, one line of help, and optional actions on the right. */
export function SectionHeader({
  id,
  actions,
  meta,
}: {
  id: SectionId;
  actions?: React.ReactNode;
  /** A line under the description, for example who can change the section. */
  meta?: React.ReactNode;
}) {
  const section = sectionById(id);
  const ref = useRef<HTMLHeadingElement>(null);
  // The focus goes to the title when the section opens, so a screen reader reads the new section.
  useEffect(() => {
    ref.current?.focus({ preventScroll: true });
  }, []);
  return (
    <header className={styles.header}>
      <div className={styles.row}>
        <h1 ref={ref} tabIndex={-1} className={styles.title}>
          {section?.title ?? id}
        </h1>
        {actions ? <div className={styles.actions}>{actions}</div> : null}
      </div>
      <p className={styles.meta}>{section?.description}</p>
      {meta ? <p className={styles.meta}>{meta}</p> : null}
    </header>
  );
}

/** A select of the agents of the org that the user sees. */
export function ProfilePicker({
  profiles,
  value,
  onChange,
}: {
  profiles: AgentProfileSummary[];
  value: string | null;
  onChange: (profileId: string) => void;
}) {
  return (
    <label className="field">
      <span className="label">Agent</span>
      <select className="input" value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        {profiles.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
    </label>
  );
}

/**
 * The card of a part of a section that another roadmap item adds. It says what comes and
 * where the user can do the work today.
 */
export function ComingCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className={`card ${styles.card}`} aria-label={title}>
      <div className={styles.cardHead}>
        <h2 className={styles.subheading}>{title}</h2>
        <span className="badge">Not available yet</span>
      </div>
      <p className={styles.text}>{children}</p>
    </section>
  );
}

export interface AgentChoice {
  org: Org;
  workspaceId: string;
  /** The agents that the workspace can use. */
  profiles: AgentProfileSummary[];
  profileId: string | null;
  setProfileId: (id: string) => void;
}

/**
 * The agent that a section works on. It starts with the first agent of the workspace, and the
 * choice stays while the agent is in the list.
 */
export function useAgentChoice(): AgentChoice {
  const { org, workspaceId } = useConsole();
  const { profilesFor } = useData();
  const [picked, setPicked] = useState<string | null>(null);
  const profiles = profilesFor(workspaceId);
  const profileId = picked !== null && profiles.some((p) => p.id === picked) ? picked : (profiles[0]?.id ?? null);
  return { org, workspaceId, profiles, profileId, setProfileId: setPicked };
}

export interface AgentProfileContext {
  profile: AgentProfile;
  /** True when the user cannot change the agent. */
  readOnly: boolean;
  /** Call it with the profile that a save answered. */
  onSaved: (profile: AgentProfile) => void;
}

/**
 * The choice of an agent and its loaded profile, for a section that edits one agent: the state
 * of no agents, the picker, the load with its loading and error states, and the read-only rule
 * (an org admin with `agents.manage`, and `profile.editable`). `children` draws the editor.
 */
export function AgentProfileEditor({
  choice,
  emptyText,
  children,
}: {
  choice: AgentChoice;
  /** Replaces the text of the state of no agents. */
  emptyText?: string;
  children: (context: AgentProfileContext) => React.ReactNode;
}) {
  const { org, profiles, profileId, setProfileId } = choice;
  if (profileId === null) {
    return (
      <div className="state">
        <h2>No agents</h2>
        <p>{emptyText ?? "This org has no agent yet. Make one in the section Harness."}</p>
      </div>
    );
  }
  return (
    <>
      <ProfilePicker profiles={profiles} value={profileId} onChange={setProfileId} />
      <LoadedProfile key={profileId} orgId={org.id} profileId={profileId} manage={canManageAgents(org)}>
        {children}
      </LoadedProfile>
    </>
  );
}

function LoadedProfile({
  orgId,
  profileId,
  manage,
  children,
}: {
  orgId: string;
  profileId: string;
  manage: boolean;
  children: (context: AgentProfileContext) => React.ReactNode;
}) {
  const api = useApi();
  const { reloadMe } = useData();
  const load = useCallback(() => api.getAgentProfile(orgId, profileId), [api, orgId, profileId]);
  const { data: profile, setData: setProfile, error, reload } = useLoad(load, "Could not load the agent.");

  if (error) return <ErrorState title="The agent did not load" message={error} onRetry={reload} />;
  if (profile === null) return <LoadingState label="Loading the agent" />;
  return (
    <>
      {children({
        profile,
        readOnly: !manage || !profile.editable,
        onSaved: (saved) => {
          setProfile(saved);
          reloadMe(); // The name and the model show in the sidebar.
        },
      })}
    </>
  );
}
