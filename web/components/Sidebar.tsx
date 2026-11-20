"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { sessionTitle } from "@/lib/chat";
import { groupByAge, relativeTime } from "@/lib/time";
import { useAuth } from "./AuthProvider";
import { useData } from "./DataProvider";
import styles from "./Sidebar.module.css";

export function Sidebar({
  open,
  modal,
  onClose,
  ref,
}: {
  open: boolean;
  /** The sidebar is an open drawer: a dialog that holds the focus. */
  modal: boolean;
  onClose: () => void;
  ref: React.Ref<HTMLElement>;
}) {
  const pathname = usePathname();
  const { signOut } = useAuth();
  const {
    me,
    meError,
    sessions,
    sessionsError,
    workspaces,
    workspaceId,
    setWorkspaceId,
    profilesFor,
    orgOfWorkspace,
    reloadMe,
    reloadSessions,
  } = useData();
  // The time that the list uses for "5 min ago". It moves once a minute.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);

  const visible = (sessions ?? []).filter((s) => workspaceId === null || s.workspace_id === workspaceId);
  const groups = groupByAge(visible, now);
  const profiles = profilesFor(workspaceId);
  const org = orgOfWorkspace(workspaceId);
  const activeSession = pathname.startsWith("/s/") ? pathname.slice(3) : null;

  return (
    <aside
      ref={ref}
      id="sidebar"
      className={`${styles.sidebar} ${open ? styles.open : ""}`}
      aria-label="Sidebar"
      role={modal ? "dialog" : undefined}
      aria-modal={modal || undefined}
    >
      <div className={styles.head}>
        <Link href="/" className={styles.brand}>
          sama
        </Link>
        <button
          type="button"
          className={`btn btn-quiet btn-icon ${styles.close}`}
          aria-label="Close the sidebar"
          onClick={onClose}
        >
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
            <path d="M4 4l10 10M14 4L4 14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      {meError ? (
        <div className={styles.block}>
          <p className="alert alert-error">{meError}</p>
          <button type="button" className="btn btn-small" onClick={reloadMe}>
            Try again
          </button>
        </div>
      ) : me === null ? (
        <div className={styles.block} aria-busy="true">
          <div className="skeleton" style={{ height: "var(--touch-target)" }} />
        </div>
      ) : workspaces.length === 0 ? (
        <div className={styles.block}>
          <p className="alert">
            You are not in a workspace yet. Ask an admin of your org to add you.
          </p>
        </div>
      ) : (
        <div className={styles.block}>
          <label className="field">
            <span className="label">Workspace</span>
            <select
              className="input"
              value={workspaceId ?? ""}
              onChange={(e) => setWorkspaceId(e.target.value)}
            >
              {workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}

      <div className={styles.block}>
        <Link href="/" className="btn btn-primary" aria-current={pathname === "/" ? "page" : undefined}>
          New session
        </Link>
      </div>

      <nav className={styles.sessions} aria-label="Sessions">
        {sessionsError && sessions === null ? (
          <div className={styles.block}>
            <p className="alert alert-error">{sessionsError}</p>
            <button type="button" className="btn btn-small" onClick={reloadSessions}>
              Try again
            </button>
          </div>
        ) : sessions === null ? (
          <div className={styles.block} aria-busy="true">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="skeleton" style={{ height: "var(--space-6)" }} />
            ))}
          </div>
        ) : visible.length === 0 ? (
          <p className={styles.empty}>No sessions yet. Start one with New session.</p>
        ) : (
          groups.map(({ group, items }) => (
            <section key={group}>
              <h2 className={styles.group}>{group}</h2>
              <ul className={styles.list}>
                {items.map((s) => (
                  <li key={s.id}>
                    <Link
                      href={`/s/${s.id}`}
                      className={styles.item}
                      aria-current={s.id === activeSession ? "page" : undefined}
                      title={sessionTitle(s)}
                    >
                      <span className={styles.itemTitle}>{sessionTitle(s)}</span>
                      <span className={styles.itemMeta}>
                        {s.audience === "shared" ? "Shared · " : ""}
                        {relativeTime(s.created_at, now)}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          ))
        )}
      </nav>

      <div className={styles.foot}>
        {profiles.length > 0 ? (
          <nav aria-label="Agents">
            <h2 className={styles.group}>Agents</h2>
            <ul className={styles.list}>
              {profiles.map((p) => (
                <li key={p.id}>
                  <Link
                    href={`/agents/${p.id}`}
                    className={styles.item}
                    aria-current={pathname === `/agents/${p.id}` ? "page" : undefined}
                  >
                    <span className={styles.itemTitle}>{p.name}</span>
                    <span className={styles.itemMeta}>{p.model}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        ) : null}
        <div className={styles.user}>
          <span className={styles.userName} title={org?.name}>
            {org ? org.display_name || org.name : ""}
          </span>
          <button type="button" className="btn btn-quiet btn-small" onClick={signOut}>
            Sign out
          </button>
        </div>
      </div>
    </aside>
  );
}
