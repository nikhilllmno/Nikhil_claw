"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { sessionTitle } from "@/lib/chat";
import { loadProgress, showSetupCard } from "@/lib/onboarding";
import { errorText, type SessionOut } from "@/lib/api";
import { anyOpen, archiveConfirmText, canManageSession, checkTitle, filterSessions, runStateLabel } from "@/lib/sessionList";
import { groupByAge, relativeTime } from "@/lib/time";
import { useApi, useAuth } from "./AuthProvider";
import { useData } from "./DataProvider";
import styles from "./Sidebar.module.css";

interface NavLink {
  href: string;
  title: string;
  /** The tooltip of the link. */
  meta: string;
  /** Only an org owner or admin sees the link. */
  admin?: boolean;
}

/** The links of the workspace, under the sessions. */
const WORKSPACE_LINKS: NavLink[] = [
  { href: "/jobs", title: "Jobs", meta: "Run the agent on a schedule" },
  { href: "/search", title: "Search", meta: "Find words in your past sessions" },
  { href: "/memory", title: "Memory", meta: "The notes that the agents keep" },
  { href: "/skills", title: "Skills", meta: "The guides that the agents follow" },
  { href: "/subagents", title: "Sub-agents", meta: "Reusable helpers for your agents" },
  { href: "/connections", title: "Connections", meta: "The tools that the agents can use" },
  { href: "/tools", title: "Tools", meta: "Your own HTTP and script tools for the agents" },
];

/** The links of the org and the account. */
const ORG_LINKS: NavLink[] = [
  { href: "/console", title: "Console", meta: "All the settings of the org, the agents, and the automation" },
  { href: "/members", title: "Members", meta: "The people of your org and their roles" },
  { href: "/admin", title: "Admin", meta: "Usage, audit log, keys, and data", admin: true },
  { href: "/billing", title: "Billing", meta: "The plan, usage, and quotas", admin: true },
  { href: "/settings", title: "Settings", meta: "Account, chat apps, agents, and your data" },
  { href: "/help", title: "Help", meta: "How to use sama" },
];

/** True when the link is the page, or a parent of the page (for example /admin of /admin/usage). */
function isCurrent(href: string, pathname: string): boolean {
  return pathname === href || (href !== "/" && pathname.startsWith(`${href}/`));
}

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
  const isOrgAdmin = me?.orgs.some((o) => o.admin) ?? false;
  // The time that the list uses for "5 min ago". It moves once a minute.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);

  const [query, setQuery] = useState("");
  const inWorkspace = (sessions ?? []).filter((s) => workspaceId === null || s.workspace_id === workspaceId);
  const visible = filterSessions(inWorkspace, query);
  const groups = groupByAge(visible, now);
  // While a session works or waits, read the list again, so that the dots stay right.
  const working = anyOpen(inWorkspace);
  useEffect(() => {
    if (!working) return;
    const timer = setInterval(() => {
      if (!document.hidden) reloadSessions();
    }, 8_000);
    return () => clearInterval(timer);
  }, [working, reloadSessions]);
  // The navigation under the sessions: closed, so that the sessions get the room, unless the
  // page is one of its links.
  const navCurrent = [...WORKSPACE_LINKS, ...ORG_LINKS].some((l) => isCurrent(l.href, pathname)) || pathname.startsWith("/agents/");
  const [navOpen, setNavOpen] = useState<boolean | null>(null);
  const profiles = profilesFor(workspaceId);
  const org = orgOfWorkspace(workspaceId);
  // A signed-in user without an org has only the welcome page, the settings, and the help.
  const noOrg = me !== null && me.orgs.length === 0;
  const activeSession = pathname.startsWith("/s/") ? (pathname.split("/")[2] ?? null) : null;

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
      ) : me.orgs.length === 0 ? (
        <div className={styles.block}>
          <p className="alert">Create an org or accept an invite to start.</p>
        </div>
      ) : workspaces.length === 0 ? (
        <div className={styles.block}>
          <p className="alert">You are not in a workspace yet. Ask an admin of your org to add you.</p>
        </div>
      ) : (
        <div className={styles.block}>
          <label className="field">
            <span className="label">Workspace</span>
            <select className="input" value={workspaceId ?? ""} onChange={(e) => setWorkspaceId(e.target.value)}>
              {workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}

      {noOrg ? (
        <div className={styles.sessions} />
      ) : (
        <>
          <div className={styles.block}>
            <Link href="/" className="btn btn-primary" aria-current={pathname === "/" ? "page" : undefined}>
              New session
            </Link>
            <Link href="/tasks" className="btn" aria-current={pathname === "/tasks" ? "page" : undefined}>
              Task board
            </Link>
          </div>

          <div className={styles.search}>
            <label className="sr-only" htmlFor="session-filter">
              Find a session
            </label>
            <input
              id="session-filter"
              className="input"
              type="search"
              placeholder="Find a session"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
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
              query.trim() ? (
                <p className={styles.empty}>
                  No title has “{query.trim()}”.{" "}
                  <Link href={`/search?q=${encodeURIComponent(query.trim())}`}>Search the messages</Link>
                </p>
              ) : (
                <p className={styles.empty}>No sessions yet. Start one with New session.</p>
              )
            ) : (
              groups.map(({ group, items }) => (
                <section key={group}>
                  <h2 className={styles.group}>{group}</h2>
                  <ul className={styles.list}>
                    {items.map((s) => (
                      <SessionRow
                        key={s.id}
                        session={s}
                        current={s.id === activeSession}
                        now={now}
                        onChanged={reloadSessions}
                      />
                    ))}
                  </ul>
                </section>
              ))
            )}
          </nav>
        </>
      )}

      <div className={styles.foot}>
        {org && org.region_here && showSetupCard(org, loadProgress(window.localStorage, org.id)) ? (
          <div className={styles.block}>
            <Link
              href="/onboarding"
              className="btn btn-small"
              aria-current={pathname === "/onboarding" ? "page" : undefined}
            >
              Finish the setup
            </Link>
          </div>
        ) : null}
        <details
          className={styles.more}
          open={navOpen ?? navCurrent}
          onToggle={(e) => setNavOpen(e.currentTarget.open)}
        >
          <summary className={styles.moreSummary}>Workspace, agents, and settings</summary>
        {noOrg ? null : <NavGroup label="Workspace" links={WORKSPACE_LINKS} pathname={pathname} admin={false} />}
        {profiles.length > 0 ? (
          <nav aria-label="Agents">
            <h2 className={styles.group}>Agents</h2>
            <ul className={styles.list}>
              {profiles.map((p) => (
                <li key={p.id}>
                  <Link
                    href={`/agents/${p.id}`}
                    className={`${styles.item} ${styles.compact}`}
                    aria-current={pathname === `/agents/${p.id}` ? "page" : undefined}
                    title={p.model}
                  >
                    <span className={styles.itemTitle}>{p.name}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        ) : null}
        <NavGroup
          label={noOrg ? "Account" : "Org"}
          links={noOrg ? ORG_LINKS.filter((l) => l.href !== "/members" && l.href !== "/console") : ORG_LINKS}
          pathname={pathname}
          admin={isOrgAdmin}
        />
        </details>
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

/** One session of the list: the title, the state, the time, and a menu with Rename and Archive. */
function SessionRow({
  session: s,
  current,
  now,
  onChanged,
}: {
  session: SessionOut;
  current: boolean;
  now: number;
  onChanged: () => void;
}) {
  const api = useApi();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const state = runStateLabel(s.run_state);
  const title = sessionTitle(s);

  const save = async () => {
    const checked = checkTitle(draft);
    if ("error" in checked) {
      setError(checked.error);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.updateSession(s.id, { title: checked.title });
      setEditing(false);
      onChanged();
    } catch (e) {
      setError(errorText(e, "The title was not saved."));
    } finally {
      setBusy(false);
    }
  };

  const archive = async () => {
    if (!window.confirm(archiveConfirmText(title))) return;
    setBusy(true);
    setError(null);
    try {
      await api.updateSession(s.id, { status: "archived" });
      onChanged();
    } catch (e) {
      setError(errorText(e, "The session was not archived."));
      setBusy(false);
    }
  };

  if (editing) {
    return (
      <li className={styles.row}>
        <form
          className={styles.rename}
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <label className="sr-only" htmlFor={`rename-${s.id}`}>
            New title
          </label>
          <input
            id={`rename-${s.id}`}
            className="input"
            value={draft}
            maxLength={200}
            autoFocus
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                setEditing(false);
              }
            }}
          />
          <button type="submit" className="btn btn-small btn-primary" disabled={busy}>
            Save
          </button>
        </form>
        {error ? <p className={styles.rowError} role="alert">{error}</p> : null}
      </li>
    );
  }

  return (
    <li className={styles.row}>
      <Link href={`/s/${s.id}`} className={styles.item} aria-current={current ? "page" : undefined} title={title}>
        <span className={styles.itemTitle}>
          {state !== null ? (
            <span className={styles.dot} data-state={s.run_state ?? undefined} aria-hidden="true" />
          ) : null}
          {title}
        </span>
        <span className={styles.itemMeta}>
          {state !== null ? `${state} · ` : ""}
          {s.audience === "shared" ? "Shared · " : ""}
          {relativeTime(s.last_activity_at ?? s.created_at, now)}
        </span>
      </Link>
      {canManageSession(s) ? (
        <details className={styles.rowMenu} data-menu="session">
          <summary className={styles.rowMenuButton} aria-label={`Actions for ${title}`}>
            <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
              <circle cx="3" cy="8" r="1.4" fill="currentColor" />
              <circle cx="8" cy="8" r="1.4" fill="currentColor" />
              <circle cx="13" cy="8" r="1.4" fill="currentColor" />
            </svg>
          </summary>
          <div className={styles.rowMenuList}>
            <button
              type="button"
              className="btn btn-quiet btn-small"
              onClick={(e) => {
                e.currentTarget.closest("details")?.removeAttribute("open");
                setDraft(s.title ?? "");
                setEditing(true);
              }}
            >
              Rename
            </button>
            <button
              type="button"
              className="btn btn-quiet btn-small"
              disabled={busy}
              onClick={(e) => {
                e.currentTarget.closest("details")?.removeAttribute("open");
                void archive();
              }}
            >
              Archive
            </button>
          </div>
        </details>
      ) : null}
      {error ? <p className={styles.rowError} role="alert">{error}</p> : null}
    </li>
  );
}

function NavGroup({
  label,
  links,
  pathname,
  admin,
}: {
  label: string;
  links: NavLink[];
  pathname: string;
  admin: boolean;
}) {
  return (
    <nav aria-label={label}>
      <h2 className={styles.group}>{label}</h2>
      <ul className={styles.list}>
        {links
          .filter((link) => admin || !link.admin)
          .map((link) => (
            <li key={link.href}>
              <Link
                href={link.href}
                className={`${styles.item} ${styles.compact}`}
                aria-current={isCurrent(link.href, pathname) ? "page" : undefined}
                title={link.meta}
              >
                <span className={styles.itemTitle}>{link.title}</span>
              </Link>
            </li>
          ))}
      </ul>
    </nav>
  );
}
