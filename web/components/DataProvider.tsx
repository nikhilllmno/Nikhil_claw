"use client";

// The data that every signed-in screen needs: the orgs, workspaces, and agents of the user
// (`GET /me`), the session list, and the workspace that the sidebar shows.

import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { AgentProfileSummary, Me, Org, SessionOut } from "@/lib/api";
import { useApi } from "./AuthProvider";
import { useLoad } from "./useLoad";
import { usePolled } from "./usePolled";
import { anyOpen } from "@/lib/sessionList";

const ACTIVE_POLL_MS = 8_000;
const IDLE_POLL_MS = 30_000;


const WORKSPACE_KEY = "sama.workspace";

export interface WorkspaceChoice {
  id: string;
  name: string;
  /** Name with the org, when the user has more than one org. */
  label: string;
}

interface Data {
  me: Me | null;
  meError: string | null;
  sessions: SessionOut[] | null;
  sessionsError: string | null;
  workspaces: WorkspaceChoice[];
  workspaceId: string | null;
  setWorkspaceId: (id: string) => void;
  /** The agents that a workspace can use: the org-wide ones and the ones of the workspace. */
  profilesFor: (workspaceId: string | null) => AgentProfileSummary[];
  orgOfWorkspace: (workspaceId: string | null) => Org | null;
  /** The org that has this agent profile. */
  orgOfProfile: (profileId: string) => Org | null;
  reloadMe: () => void;
  reloadSessions: () => void;
}

const Context = createContext<Data | null>(null);

export function useData(): Data {
  const value = useContext(Context);
  if (value === null) throw new Error("useData needs a DataProvider.");
  return value;
}

export function DataProvider({ children }: { children: React.ReactNode }) {
  const api = useApi();
  const [selected, setSelected] = useState<string | null>(null);
  const loadMe = useCallback(() => api.me(), [api]);
  // The archive of a compacted transcript is not a session to open from the list.
  const loadSessions = useCallback(
    async () => (await api.listSessions()).filter((s) => s.lineage !== "compaction"),
    [api],
  );
  const { data: me, error: meError, reload: reloadMe } = useLoad(loadMe, "Could not load your account.");
  // The list polls one request at a time (M8.2): every 8 s while a session works or waits, so
  // that the sidebar dots stay right, else every 30 s, so that a run from another tab or
  // channel shows too.
  const [sessionsActive, setSessionsActive] = useState(false);
  const {
    data: sessions,
    error: sessionsError,
    reload: reloadSessions,
  } = usePolled(loadSessions, "Could not load sessions.", sessionsActive ? ACTIVE_POLL_MS : IDLE_POLL_MS);
  const active = sessions !== null && anyOpen(sessions);
  if (active !== sessionsActive) setSessionsActive(active);

  const workspaces = useMemo<WorkspaceChoice[]>(() => {
    const orgs = me?.orgs ?? [];
    return orgs.flatMap((org) =>
      org.workspaces.map((w) => ({
        id: w.id,
        name: w.name,
        label: orgs.length > 1 ? `${org.name} / ${w.name}` : w.name,
      })),
    );
  }, [me]);

  const workspaceId = useMemo(() => {
    if (selected !== null && workspaces.some((w) => w.id === selected)) return selected;
    const saved = typeof window === "undefined" ? null : window.localStorage.getItem(WORKSPACE_KEY);
    if (saved !== null && workspaces.some((w) => w.id === saved)) return saved;
    return workspaces[0]?.id ?? null;
  }, [selected, workspaces]);

  const setWorkspaceId = useCallback((id: string) => {
    setSelected(id);
    window.localStorage.setItem(WORKSPACE_KEY, id);
  }, []);

  const orgOfWorkspace = useCallback(
    (id: string | null) => me?.orgs.find((o) => o.workspaces.some((w) => w.id === id)) ?? null,
    [me],
  );
  const orgOfProfile = useCallback(
    (id: string) => me?.orgs.find((o) => o.agent_profiles.some((p) => p.id === id)) ?? null,
    [me],
  );
  const profilesFor = useCallback(
    (id: string | null) =>
      orgOfWorkspace(id)?.agent_profiles.filter((p) => p.workspace_id === null || p.workspace_id === id) ??
      [],
    [orgOfWorkspace],
  );

  const value = useMemo<Data>(
    () => ({
      me,
      meError,
      sessions,
      sessionsError,
      workspaces,
      workspaceId,
      setWorkspaceId,
      profilesFor,
      orgOfWorkspace,
      orgOfProfile,
      reloadMe,
      reloadSessions,
    }),
    [
      me,
      meError,
      sessions,
      sessionsError,
      workspaces,
      workspaceId,
      setWorkspaceId,
      profilesFor,
      orgOfWorkspace,
      orgOfProfile,
      reloadMe,
      reloadSessions,
    ],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
